import asyncio
import rclpy
import tf2_geometry_msgs
import tf2_ros
import time
import utm
from action_msgs.msg import GoalStatus
from aruco_opencv_msgs.msg import ArucoDetection
from builtin_interfaces.msg import Duration
from geometry_msgs.msg import Pose
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import FollowGPSWaypoints, Spin
from nav2_simple_commander.robot_navigator import TaskResult
from rclpy.action import ActionServer, ActionClient, CancelResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future
from rover_interfaces.action import RunTask
from sensor_msgs.msg import NavSatFix
from std_srvs.srv import Trigger
from threading import RLock
from typing import Any
from zed_msgs.msg import ObjectsStamped, Object

from rover_navigation.utils.gps_utils import latLonYaw2Geopose
from rover_navigation.utils.yaml_utils import waypointParser
from rover_navigation.utils.plan_utils import (
    basicPathPlanner,  # plan a straight line between two GPS coordinates
    bruteOrderPlanner,  # use brute force to find the best order of legs
    greedyOrderPlanner,  # use a greedy algorithm to find the best order of legs
    noOrderPlanner,  # don't reorder the legs
)
from rover_navigation.utils.terrain_utils import (
    terrainPathPlanner,
    terrainOrderPlanner,
)


class PatchRclpyIssue1123(ActionClient):
    """
    ActionClient patch for rclpy timing issue when multi-threading
    https://github.com/ros2/rclpy/issues/1123
    """

    _lock: RLock = None  # type: ignore

    @property
    def _cpp_client_handle_lock(self) -> RLock:
        if self._lock is None:
            self._lock = RLock()
        return self._lock

    async def execute(self, *args: Any, **kwargs: Any) -> None:
        with self._cpp_client_handle_lock:
            return await super().execute(*args, **kwargs)  # type: ignore

    def send_goal_async(self, *args: Any, **kwargs: Any) -> Future:
        with self._cpp_client_handle_lock:
            return super().send_goal_async(*args, **kwargs)

    def _cancel_goal_async(self, *args: Any, **kwargs: Any) -> Future:
        with self._cpp_client_handle_lock:
            return super()._cancel_goal_async(*args, **kwargs)

    def _get_result_async(self, *args: Any, **kwargs: Any) -> Future:
        with self._cpp_client_handle_lock:
            return super()._get_result_async(*args, **kwargs)


class AutonomyTaskExecutor(Node):
    """
    Class for executing the autonomy task using the Nav2 stack

    Note: This is a pretty complex node. It's a hacked-together combination of the BasicNavigator
    class and our own custom task executor with a lot of multi-threading. It's easiest to think of
    as three sections with their own seperate threads: the task executor, the Nav2 BasicNavigator,
    and the ROS 2 callbacks.

    :author: Nelson Durrant
    :date: Mar 2025

    Subscribers:
    - gps/filtered (sensor_msgs/NavSatFix) [norm_callback_group]
    - aruco_detections (aruco_opencv_msgs/ArucoDetection) [norm_callback_group]
    - obj_det/objects (zed_msgs/ObjectsStamped) [norm_callback_group]
    Publishers:
    - mapviz/goal (sensor_msgs/NavSatFix)
    - mapviz/inter (sensor_msgs/NavSatFix)
    Clients:
    - trigger_teleop (std_srvs/Trigger) [norm_callback_group]
    - trigger_auto (std_srvs/Trigger) [norm_callback_group]
    - trigger_arrival (std_srvs/Trigger) [norm_callback_group]
    Action Clients:
    - follow_gps_waypoints (nav2_msgs/FollowGPSWaypoints) [basic_nav_callback_group]
    - spin (nav2_msgs/Spin) [basic_nav_callback_group]
    - {node_name}/get_state (lifecycle_msgs/GetState) [basic_nav_callback_group] (temporary)
    Action Servers:
    - exec_autonomy_task (rover_interfaces/RunTask) [action_callback_group]
    *And a tf2 buffer and listener for pose to GPS transforms
    """

    def __init__(self):

        super().__init__("task_executor")
        # self.navigator = BasicNavigator() # Don't uncomment this line
        # IMPORTANT! Simply using the BasicNavigator class causes A LOT of threading issues.
        # We've hacked the relevant functions from the BasicNavigator class into this class as a fix.
        # https://github.com/ros-navigation/navigation2/tree/main/nav2_simple_commander

        # Declare waypoint file path parameter
        self.declare_parameter("wps_file_path", "")
        if not self.get_parameter("wps_file_path").value:
            self.get_logger().fatal("No waypoint file path provided")
            rclpy.shutdown()

        # Declare the parameter for the path and leg order planners
        self.declare_parameter("path_planner", "basicPathPlanner")
        self.declare_parameter("order_planner", "greedyOrderPlanner")
        self.path_planner = self.get_parameter("path_planner").value
        self.order_planner = self.get_parameter("order_planner").value

        # Initialize variables
        self.leg = None
        self.cancel_flag = False

        # Task constants
        self.gps_legs = ["gps1", "gps2"]
        self.aruco_legs = ["aruco1", "aruco2", "aruco3"]
        self.obj_legs = ["mallet", "bottle"]
        self.tags = {"aruco1": 1, "aruco2": 2, "aruco3": 3}

        # UTM zone and hemisphere (will set on first gps fix)
        self.zone = None
        self.hemisphere = None
        self.filtered_gps = None

        # Tunable values
        self.wait_time = 10  # Time to wait after arrival
        self.update_threshold = (
            0.000005  # Threshold for updating tag and item locations
        )

        # Hex pattern for searching
        self.hex_coord = [
            (1.0, 0.0),
            (0.5, 0.866),
            (-0.5, 0.866),
            (-1.0, 0.0),
            (-0.5, -0.866),
            (0.5, -0.866),
        ]
        self.hex_scalar = 0.0001

        #################################
        ### ROS 2 OBJECT DECLARATIONS ###
        #################################

        self.cancel_flag = False  # monitor action cancel requests

        # Set up a Tf2 buffer for pose to GPS transforms (aruco, object)
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer, self)

        # Callback groups (for threading)
        norm_callback_group = MutuallyExclusiveCallbackGroup()
        action_callback_group = (
            ReentrantCallbackGroup()
        )  # needed to monitor cancel requests

        # Filtered GPS location subscriber
        self.gps_subscriber = self.create_subscription(
            NavSatFix,
            "gps/filtered",
            self.gps_callback,
            10,
            callback_group=norm_callback_group,
        )
        self.gps_subscriber  # prevent unused variable warning

        # Aruco detection pose subscriber
        self.aruco_subscriber = self.create_subscription(
            ArucoDetection,
            "aruco_detections",
            self.aruco_callback,
            10,
            callback_group=norm_callback_group,
        )
        self.aruco_subscriber  # prevent unused variable warning

        # Object detection pose subscriber
        self.obj_subscriber = self.create_subscription(
            ObjectsStamped,
            "obj_det/objects",
            self.obj_callback,
            10,
            callback_group=norm_callback_group,
        )
        self.obj_subscriber  # prevent unused variable warning

        # Mapviz publishers (to show the GPS destinations in mapviz)
        self.mapviz_goal_publisher = self.create_publisher(NavSatFix, "mapviz/goal", 10)
        self.mapviz_inter_publisher = self.create_publisher(
            NavSatFix, "mapviz/inter", 10
        )

        # Client to trigger teleop state
        self.teleop_client = self.create_client(
            Trigger, "trigger_teleop", callback_group=norm_callback_group
        )
        while not self.teleop_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info(
                "Teleop trigger service not available, waiting again..."
            )
        self.teleop_request = Trigger.Request()

        # Client to trigger autonomy state
        self.auto_client = self.create_client(
            Trigger, "trigger_auto", callback_group=norm_callback_group
        )
        while not self.auto_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info(
                "Autonomy trigger service not available, waiting again..."
            )
        self.auto_request = Trigger.Request()

        # Client to trigger arrival state
        self.arrival_client = self.create_client(
            Trigger, "trigger_arrival", callback_group=norm_callback_group
        )
        while not self.arrival_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info(
                "Arrival trigger service not available, waiting again..."
            )
        self.arrival_request = Trigger.Request()

        # Action server to run the task executor
        self.action_server = ActionServer(
            self,
            RunTask,
            "exec_autonomy_task",
            self.action_server_callback,
            callback_group=action_callback_group,
            cancel_callback=self.cancel_callback,
        )

        #####################################
        ### END ROS 2 OBJECT DECLARATIONS ###
        #####################################

        #######################################
        ### NAV2 BASIC NAVIGATOR BASED CODE ###
        #######################################

        self.goal_handle = None
        self.result_future = None
        self.feedback = None
        self.status = None

        self.basic_nav_callback_group = MutuallyExclusiveCallbackGroup()
        self.follow_gps_waypoints_client = PatchRclpyIssue1123(
            self,
            FollowGPSWaypoints,
            "follow_gps_waypoints",
            callback_group=self.basic_nav_callback_group,
        )
        self.spin_client = PatchRclpyIssue1123(
            self, Spin, "spin", callback_group=self.basic_nav_callback_group
        )

        ###########################################
        ### END NAV2 BASIC NAVIGATOR BASED CODE ###
        ###########################################

        self.get_logger().info("Autonomy task executor node initialized")

    #######################################
    ### NAV2 BASIC NAVIGATOR BASED CODE ###
    #######################################

    async def followGpsWaypoints(self, gps_poses):
        """
        Function to follow a set of GPS waypoints, based on the nav2_simple_commander code
        NOTE: Call this with the asyncio.run() function
        """

        self.debug("Waiting for 'FollowWaypoints' action server")
        while not self.follow_gps_waypoints_client.wait_for_server(timeout_sec=1.0):
            self.info("'FollowWaypoints' action server not available, waiting...")

        goal_msg = FollowGPSWaypoints.Goal()
        goal_msg.gps_poses = gps_poses

        self.info(f"Following {len(goal_msg.gps_poses)} gps goals....")
        send_goal_future = self.follow_gps_waypoints_client.send_goal_async(
            goal_msg, self._feedbackCallback
        )
        await send_goal_future  # fix for iron threading bug
        self.goal_handle = send_goal_future.result()

        if not self.goal_handle.accepted:
            self.error("FollowWaypoints request was rejected!")
            return False

        self.result_future = self.goal_handle.get_result_async()
        return True

    async def spin(
        self, spin_dist=1.57, time_allowance=10, disable_collision_checks=False
    ):
        """
        Function to spin in place, based on the nav2_simple_commander code
        NOTE: Call this with the asyncio.run() function
        """

        self.debug("Waiting for 'Spin' action server")
        while not self.spin_client.wait_for_server(timeout_sec=1.0):
            self.info("'Spin' action server not available, waiting...")
        goal_msg = Spin.Goal()
        goal_msg.target_yaw = spin_dist
        goal_msg.time_allowance = Duration(sec=time_allowance)
        # disable_collision_checks isn't in the iron release of Nav2
        # goal_msg.disable_collision_checks = disable_collision_checks

        self.info(f"Spinning to angle {goal_msg.target_yaw}....")
        send_goal_future = self.spin_client.send_goal_async(
            goal_msg, self._feedbackCallback
        )
        await send_goal_future  # fix for iron threading bug
        self.goal_handle = send_goal_future.result()

        if not self.goal_handle.accepted:
            self.error("Spin request was rejected!")
            return False

        self.result_future = self.goal_handle.get_result_async()
        return True

    async def cancelTask(self):
        """
        Cancel pending task request of any type, based on the nav2_simple_commander code
        NOTE: Call this with the asyncio.run() function
        """

        self.info("Canceling current task.")
        if self.result_future:
            future = self.goal_handle.cancel_goal_async()
            await future  # fix for iron threading bug
        return

    async def isTaskComplete(self):
        """
        Check if the task request of any type is complete yet, based on the nav2_simple_commander code
        NOTE: Call this with the asyncio.run() function
        """

        if not self.result_future:
            # task was cancelled or completed
            return True

        # Fix for iron threading bug (with timeout)
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for
        try:
            await asyncio.wait_for(self.isTaskCompleteHelper(), timeout=0.1)
        except asyncio.TimeoutError:
            self.debug("Timed out waiting for async future to complete")

        if self.result_future.result():
            self.status = self.result_future.result().status
            if self.status != GoalStatus.STATUS_SUCCEEDED:
                self.debug(f"Task with failed with status code: {self.status}")
                return True
        else:
            # Timed out, still processing, not complete yet
            return False

        self.debug("Task succeeded!")
        return True

    async def isTaskCompleteHelper(self):
        """
        Helper function for async 'wait_for' wrapping
        """

        await self.result_future

    def getFeedback(self):
        """
        Get the pending action feedback message, based on the nav2_simple_commander code
        """

        return self.feedback

    def getResult(self):
        """
        Get the pending action result message, based on the nav2_simple_commander code
        """

        if self.status == GoalStatus.STATUS_SUCCEEDED:
            return TaskResult.SUCCEEDED
        elif self.status == GoalStatus.STATUS_ABORTED:
            return TaskResult.FAILED
        elif self.status == GoalStatus.STATUS_CANCELED:
            return TaskResult.CANCELED
        else:
            return TaskResult.UNKNOWN

    def waitUntilNav2Active(self, navigator="bt_navigator", localizer="amcl"):
        """
        Block until the full navigation system is up and running, based on the nav2_simple_commander code
        """

        if localizer != "robot_localization":  # non-lifecycle node
            asyncio.run(self._waitForNodeToActivate(localizer))
        # if localizer == 'amcl':
        #     self._waitForInitialPose()
        asyncio.run(self._waitForNodeToActivate(navigator))
        self.info("Nav2 is ready for use!")
        return

    async def _waitForNodeToActivate(self, node_name):
        """
        Waits for the node within the tester namespace to become active, based on the nav2_simple_commander code
        NOTE: Call this with the asyncio.run() function
        """

        self.debug(f"Waiting for {node_name} to become active..")
        node_service = f"{node_name}/get_state"
        state_client = self.create_client(
            GetState, node_service, callback_group=self.basic_nav_callback_group
        )
        while not state_client.wait_for_service(timeout_sec=1.0):
            self.info(f"{node_service} service not available, waiting...")

        req = GetState.Request()
        state = "unknown"
        while state != "active":
            self.debug(f"Getting {node_name} state...")
            future = state_client.call_async(req)
            await future  # fix for iron threading bug
            if future.result() is not None:
                state = future.result().current_state.label
                self.debug(f"Result of get_state: {state}")
            time.sleep(2)
        return

    def _feedbackCallback(self, msg):
        self.debug("Received action feedback message")
        self.feedback = msg.feedback
        return

    def info(self, msg):
        self.get_logger().info(msg)
        return

    def warn(self, msg):
        self.get_logger().warn(msg)
        return

    def error(self, msg):
        self.get_logger().error(msg)
        return

    def debug(self, msg):
        self.get_logger().debug(msg)
        return

    ###########################################
    ### END NAV2 BASIC NAVIGATOR BASED CODE ###
    ###########################################

    #######################
    ### ROS 2 CALLBACKS ###
    #######################

    def cancel_callback(self, goal_handle):
        """
        Callback function for the action server cancel request
        """

        self.cancel_flag = True
        return CancelResponse.ACCEPT

    async def async_service_call(self, client, request):
        """
        Fix for iron threading bug - https://github.com/ros2/rclpy/issues/1337
        NOTE: Call this with the asyncio.run() function (and all other async functions)
        """

        future = client.call_async(request)
        await future

    def action_server_callback(self, goal_handle):
        """
        Callback function for the action server
        """

        # Parse the current waypoint file path
        wps_file_path = self.get_parameter("wps_file_path").value
        self.wps = waypointParser(wps_file_path)

        self.task_goal_handle = goal_handle
        self.cancel_flag = False
        result = RunTask.Result()

        # Initialize variables
        self.legs = []
        self.leg = "start"
        self.found_poses = {
            "aruco1": None,
            "aruco2": None,
            "aruco3": None,
            "mallet": None,
            "bottle": None,
        }

        # Get the task legs from the goal
        self.legs = goal_handle.request.legs
        if not self.legs:
            self.task_fatal("No task legs provided")
            result.msg = "Well that was less-than-interstellar of you"
            self.task_goal_handle.abort()
            return result

        # Trigger the autonomy state
        asyncio.run(self.async_service_call(self.auto_client, self.auto_request))

        try:
            self.exec_autonomy_task()
            result.msg = "One small step for a rover, one giant leap for roverkind"
            self.task_goal_handle.succeed()
        except Exception as e:  # catch exceptions to ensure we return to teleop state
            self.task_fatal(str(e))
            result.msg = "It was the aliens, I'm telling you"
            self.task_goal_handle.abort()

        # Trigger the teleop state
        asyncio.run(self.async_service_call(self.teleop_client, self.teleop_request))

        return result

    def gps_callback(self, msg):
        """
        Callback function for the GPS subscriber
        """

        # Set zone and hemisphere for UTM conversions
        if self.zone is None or self.hemisphere is None:
            self.zone = utm.from_latlon(msg.latitude, msg.longitude)[2]
            self.hemisphere = "N" if msg.latitude > 0 else "S"

        self.filtered_gps = latLonYaw2Geopose(msg.latitude, msg.longitude)

    def pose_to_geopose(self, pose, frame_id, stamp):
        """
        Convert a pose to a geopose we can navigate to
        """

        # Look up and use the transform to convert the pose to UTM
        try:
            tf = self.tf_buffer.lookup_transform("utm", frame_id, stamp)
            utm_pose = tf2_geometry_msgs.do_transform_pose(pose, tf)
        except Exception as e:
            self.get_logger().warn(f"Could not transform pose: {e}")
            return False

        # Check to make sure we've had at least one GPS fix
        if self.filtered_gps is None:
            self.get_logger().error("No filtered GPS fix available for UTM conversion")
            return False

        # Given UTM pose, convert to GPS
        lat, lon = utm.to_latlon(
            utm_pose.position.x,
            utm_pose.position.y,
            self.zone,
            self.hemisphere,
        )

        return latLonYaw2Geopose(lat, lon)

    def aruco_callback(self, msg):
        """
        Callback function for the aruco pose subscriber
        """

        if self.leg in self.aruco_legs:
            for marker in msg.markers:
                # Are we looking for this marker right now?
                if marker.marker_id == self.tags[self.leg]:

                    self.get_logger().info(f"Found aruco tag {marker.marker_id}")

                    # Convert the pose to a GeoPose
                    pose = self.pose_to_geopose(
                        marker.pose, msg.header.frame_id, msg.header.stamp
                    )

                    # If it was successful, store it
                    if pose:
                        self.found_poses[self.leg] = pose

    def obj_callback(self, msg):
        """
        Callback function for the object pose subscriber
        """

        if self.leg in self.obj_legs:
            for obj in msg.objects:
                # Are we looking for this object right now?
                if obj.label in self.leg:

                    self.get_logger().info(f"Found object {obj.label}")

                    # Convert to a Pose() message
                    pose = Pose()
                    pose.position.x = obj.position[0]
                    pose.position.y = obj.position[1]
                    pose.position.z = obj.position[2]
                    pose.orientation.x = 0.0
                    pose.orientation.y = 0.0
                    pose.orientation.z = 0.0
                    pose.orientation.w = 1.0

                    # Convert the pose to a GeoPose
                    pose = self.pose_to_geopose(
                        pose, msg.header.frame_id, msg.header.stamp
                    )

                    # If it was successful, store it
                    if pose:
                        self.found_poses[self.leg] = pose

    ###########################
    ### END ROS 2 CALLBACKS ###
    ###########################

    ####################################
    ### TASK EXEC FEEDBACK FUNCTIONS ###
    ####################################

    def task_info(self, string):
        """
        Function to write info back to the RunTask action client
        """

        self.get_logger().info("[" + self.leg + "] " + string)
        task_feedback = RunTask.Feedback()
        task_feedback.status = "[" + self.leg + "] " + string
        self.task_goal_handle.publish_feedback(task_feedback)

    def task_warn(self, string):
        """
        Function to write warnings back to the RunTask action client
        """

        self.get_logger().warn("[" + self.leg + "] " + string)
        task_feedback = RunTask.Feedback()
        task_feedback.status = "[WARN] [" + self.leg + "] " + string
        self.task_goal_handle.publish_feedback(task_feedback)

    def task_error(self, string):
        """
        Function to write errors back to the RunTask action client
        """

        self.get_logger().error("[" + self.leg + "] " + string)
        task_feedback = RunTask.Feedback()
        task_feedback.status = "[ERROR] [" + self.leg + "] " + string
        self.task_goal_handle.publish_feedback(task_feedback)

    def task_fatal(self, string):
        """
        Function to write fatal errors back to the RunTask action client
        """

        self.get_logger().fatal("[" + self.leg + "] " + string)
        task_feedback = RunTask.Feedback()
        task_feedback.status = "[FATAL] [" + self.leg + "] " + string
        self.task_goal_handle.publish_feedback(task_feedback)

    ########################################
    ### END TASK EXEC FEEDBACK FUNCTIONS ###
    ########################################

    ###############################
    ### TASK EXECUTOR FUNCTIONS ###
    ###############################

    def exec_autonomy_task(self):
        """
        Function to execute the autonomy task
        """

        self.task_info("Autonomy task execution started")

        # Check for the first GPS fix
        while self.filtered_gps is None:
            time.sleep(1)
            self.task_warn("Waiting on a GPS fix...")

            # Check if the goal has been canceled
            if self.task_goal_handle.is_cancel_requested:
                asyncio.run(self.cancelTask())
                raise Exception("Task execution canceled by action client")

        self.task_info(
            "Using order planner: "
            + self.order_planner
            + " and path planner: "
            + self.path_planner
        )

        self.task_info("Please review the leg order plan (exit matplotlib to continue)")

        # Determine the best order for the legs
        if self.order_planner == "bruteOrderPlanner":
            self.legs = bruteOrderPlanner(self.legs, self.wps, self.filtered_gps)
        elif self.order_planner == "greedyOrderPlanner":
            self.legs = greedyOrderPlanner(self.legs, self.wps, self.filtered_gps)
        elif self.order_planner == "terrainOrderPlanner":
            self.legs = terrainOrderPlanner(self.legs, self.wps, self.filtered_gps)
        elif self.order_planner == "noOrderPlanner":
            self.legs = noOrderPlanner(self.legs, self.wps, self.filtered_gps)
        else:
            raise Exception("Invalid order planner provided: " + self.order_planner)

        self.task_info("Determined best leg order: " + str(self.legs))

        self.waitUntilNav2Active(localizer="robot_localization")

        for leg in self.legs:
            self.exec_leg(leg)

        self.leg = "end"
        self.task_info("Autonomy task execution completed")

    def exec_leg(self, leg):
        """
        Function to execute task legs

        :param leg: The leg to execute
        """

        self.leg = leg

        # Is it a valid leg type?
        if (
            self.leg in self.gps_legs
            or self.leg in self.aruco_legs
            or self.leg in self.obj_legs
        ):

            if self.leg in self.gps_legs:
                print_string = "GPS waypoint"
            elif self.leg in self.aruco_legs:
                print_string = "aruco tag"
            elif self.leg in self.obj_legs:
                print_string = "object"

            self.task_info("Starting " + print_string + " leg")

            # Get this leg's GPS waypoint
            leg_wp = None
            for wp in self.wps:
                if wp["leg"] == self.leg:
                    leg_wp = latLonYaw2Geopose(wp["latitude"], wp["longitude"])

            if not leg_wp:
                self.task_error("No GPS waypoint found for leg")
                return

            found_loc = self.gps_nav(leg_wp)  # look along the way

            # Do we need to look for an aruco tag or object?
            if self.leg in self.aruco_legs or self.leg in self.obj_legs:

                # Check for the aruco tag or object until found
                if not found_loc:
                    found_loc = self.spin_search()  # Do a spin search
                if not found_loc:
                    found_loc = self.hex_search()  # Do a hex search
                if not found_loc:
                    self.task_error("Could not find the " + print_string)
                    return
                else:
                    self.task_info("Found the " + print_string + "!")

                    # Change found GPS location if we get a better one as we move closer
                    while found_loc:
                        found_loc = self.gps_nav(
                            found_loc, " (" + print_string + ")", updating=True
                        )

                    self.task_info("SUCCESS! Found and navigated to " + print_string)

            else:
                self.task_info("SUCCESS! Navigated to " + print_string)

            self.task_info("Flashing LED to indicate arrival")

            # Trigger the arrival state
            asyncio.run(
                self.async_service_call(self.arrival_client, self.arrival_request)
            )

            time.sleep(self.wait_time)

            # Trigger the autonomy state
            asyncio.run(self.async_service_call(self.auto_client, self.auto_request))

        else:
            self.task_error("Invalid leg type provided:" + self.leg)

    def gps_nav(self, dest_wp, src_string="", updating=False):
        """
        Function to navigate through GPS waypoints

        :param dest_wp: The destination waypoint
        :param src_string: A string to append to the task info
        :param updating: Are we already navigating to an object and should check for a better location?

        :return: The improved GPS location (if updating) or a found GPS location, False otherwise
        """

        self.task_info("Starting GPS navigation" + src_string)

        # Generate a path to the destination waypoint
        if self.path_planner == "basicPathPlanner":
            path = basicPathPlanner(self.filtered_gps, dest_wp)
        elif self.path_planner == "terrainPathPlanner":
            path = terrainPathPlanner(self.filtered_gps, dest_wp)
        else:
            raise Exception("Invalid path planner provided: " + self.path_planner)

        # Publish the GPS positions to mapviz
        for wp in path:
            navsat_fix = NavSatFix()
            navsat_fix.header.frame_id = "map"
            navsat_fix.header.stamp = self.get_clock().now().to_msg()
            navsat_fix.latitude = wp.position.latitude
            navsat_fix.longitude = wp.position.longitude

            # Publish to different topics based on type
            if wp != dest_wp:
                self.mapviz_inter_publisher.publish(navsat_fix)
            else:
                self.mapviz_goal_publisher.publish(navsat_fix)

        asyncio.run(self.followGpsWaypoints(path))
        while not asyncio.run(self.isTaskComplete()):
            time.sleep(0.1)

            # Check if the goal has been canceled
            if self.task_goal_handle.is_cancel_requested:
                asyncio.run(self.cancelTask())
                raise Exception("Task execution canceled by action client")

            # See if we get a better pose from the aruco tag or object
            if updating:
                pose = self.found_check()

                # Check if its location has changed by a significant amount
                if (
                    abs(pose.position.latitude - dest_wp.position.latitude)
                    > self.update_threshold
                ) or (
                    abs(pose.position.longitude - dest_wp.position.longitude)
                    > self.update_threshold
                ):
                    self.task_info("Improved GPS location found" + src_string)
                    asyncio.run(self.cancelTask())
                    time.sleep(0.5)  # give the action server time to cancel
                    return pose  # restart gps_nav with the new location
            else:
                # Check for the aruco tag or object while navigating
                pose = self.found_check()
                if pose:
                    asyncio.run(self.cancelTask())
                    return pose

        result = self.getResult()
        if result == TaskResult.SUCCEEDED:
            self.task_info("GPS navigation completed" + src_string)
        elif result == TaskResult.CANCELED:
            self.task_warn("GPS navigation canceled" + src_string)
        elif result == TaskResult.FAILED:
            self.task_error("GPS navigation failed" + src_string)
        return False

    def spin_search(self, src_string=""):
        """
        Function to perform a spin search

        :param src_string: A string to append to the task info

        :return: The found GPS location, False otherwise
        """

        self.task_info("Starting spin search" + src_string)

        asyncio.run(self.spin(spin_dist=3.14))
        while not asyncio.run(self.isTaskComplete()):
            time.sleep(0.1)

            # Check if the goal has been canceled
            if self.task_goal_handle.is_cancel_requested:
                asyncio.run(self.cancelTask())
                raise Exception("Task execution canceled by action client")

            # Check for the aruco tag or object
            pose = self.found_check()
            if pose:
                asyncio.run(self.cancelTask())
                return pose

        result = self.getResult()
        if result == TaskResult.SUCCEEDED:
            self.task_info("Spin search completed" + src_string)
        elif result == TaskResult.CANCELED:
            self.task_warn("Spin search canceled" + src_string)
        elif result == TaskResult.FAILED:
            self.task_error("Spin search failed" + src_string)
        return False

    def hex_search(self):
        """
        Function to search in a hex pattern

        :return: The found GPS location, False otherwise
        """

        self.task_info("Starting hex search")

        # Get the base waypoint
        for wp in self.wps:
            if wp["leg"] == self.leg:
                base_wp = latLonYaw2Geopose(wp["latitude"], wp["longitude"])

        # Generate a hex pattern from the base waypoint
        for i, coord in enumerate(self.hex_coord):
            hex_lat = base_wp.position.latitude + coord[0] * self.hex_scalar
            hex_lon = base_wp.position.longitude + coord[1] * self.hex_scalar
            hex_wp = latLonYaw2Geopose(hex_lat, hex_lon)

            pose = self.gps_nav(hex_wp, " (hex " + str(i) + ")")
            # Did the last gps_nav find it?
            if pose:
                return pose

            pose = self.spin_search(" (hex " + str(i) + ")")  # Do a spin search
            # Did the last spin_search find it?
            if pose:
                return pose

        self.task_info("Hex search completed")
        return False

    def found_check(self):
        """
        Function to check for aruco tags and objects

        :return: The found GPS location, False otherwise
        """

        if self.leg in self.aruco_legs or self.leg in self.obj_legs:
            if self.found_poses[self.leg]:
                return self.found_poses[self.leg]
        return False

    ###################################
    ### END TASK EXECUTOR FUNCTIONS ###
    ###################################


def main(args=None):
    rclpy.init(args=args)

    task_executor = AutonomyTaskExecutor()
    # Create a multi-threaded node executor for callback-in-callback threading
    executor = MultiThreadedExecutor()
    executor.add_node(task_executor)

    executor.spin()


if __name__ == "__main__":
    main()
