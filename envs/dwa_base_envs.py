import gym
import rospy
import rospkg
import roslaunch
import time
import numpy as np
import os
import cv2
from os.path import dirname, join, abspath
import subprocess
from gym.spaces import Box, Discrete

from envs.gazebo_simulation import GazeboSimulation
from envs.move_base import MoveBase


class DWABase(gym.Env):
    def __init__(
        self,
        base_local_planner="base_local_planner/TrajectoryPlannerROS",
        world_name="jackal_world.world",
        gui=False,
        init_position=[0, 0, 0],
        goal_position=[4, 0, 0],
        max_step=100,
        time_step=1,
        slack_reward=-1,
        failure_reward=-50,
        success_reward=0,
        verbose=True
    ):
        """Base RL env that initialize jackal simulation in Gazebo
        """
        super().__init__()

        self.base_local_planner = base_local_planner
        self.world_name = world_name
        self.gui = gui
        self.init_position = init_position
        self.goal_position = goal_position
        self.verbose = verbose
        self.time_step = time_step
        self.max_step = max_step
        self.slack_reward = slack_reward
        self.failure_reward = failure_reward
        self.success_reward = success_reward

        # launch gazebo and dwa demo
        rospy.logwarn(">>>>>>>>>>>>>>>>>> Load world: %s <<<<<<<<<<<<<<<<<<" %(world_name))
        rospack = rospkg.RosPack()
        self.BASE_PATH = rospack.get_path('jackal_helper')
        world_name = join(self.BASE_PATH, "worlds", world_name)
        launch_file = join(self.BASE_PATH, 'launch', 'ros_jackal_launch.launch')

        self.gazebo_process = subprocess.Popen(['roslaunch', 
                                                launch_file,
                                                'world_name:=' + world_name,
                                                'gui:=' + ("true" if gui else "false"),
                                                'verbose:=' + ("true" if verbose else "false"),
                                                'base_local_planner:=' + base_local_planner
                                                ])
        time.sleep(10)  # sleep to wait until the gazebo being created

        # initialize the node for gym env
        rospy.init_node('gym', anonymous=True, log_level=rospy.FATAL)
        rospy.set_param('/use_sim_time', True)

        self._set_start_goal_BARN(self.world_name)  # overwrite the starting goal if use BARN dataset
        self.gazebo_sim = GazeboSimulation(init_position=self.init_position)
        self.move_base = MoveBase(goal_position=self.goal_position, base_local_planner=base_local_planner)

        # Not implemented
        self.action_space = None
        self.observation_space = None
        self.reward_range = (
            min(slack_reward, failure_reward), 
            success_reward
        )

        self.step_count = 0

    def seed(self, seed):
        np.random.seed(seed)

    def reset(self):
        """reset the environment
        """
        self.step_count=0
        # Reset robot in odom frame clear_costmap
        self.gazebo_sim.unpause()
        self.move_base.reset_robot_in_odom()
        # Resets the state of the environment and returns an initial observation
        self.gazebo_sim.reset()
        self.move_base.set_global_goal()
        self._clear_costmap()
        self.start_time = rospy.get_time()
        obs = self._get_observation()
        self.gazebo_sim.pause()
        return obs

    def _clear_costmap(self):
        self.move_base.clear_costmap()
        rospy.sleep(0.1)
        self.move_base.clear_costmap()
        rospy.sleep(0.1)
        self.move_base.clear_costmap()

    def step(self, action):
        """take an action and step the environment
        """
        self._take_action(action)
        self.step_count += 1
        self.gazebo_sim.unpause()
        obs = self._get_observation()
        rew = self._get_reward()
        done = self._get_done()
        info = self._get_info()
        self.gazebo_sim.pause()
        return obs, rew, done, info

    def _take_action(self, action):
        raise NotImplementedError()

    def _get_observation(self):
        raise NotImplementedError()

    def _get_success(self):
        # check the robot distance to the goal position
        robot_position = np.array([self.move_base.robot_config.X, 
                                   self.move_base.robot_config.Y]) # robot position in odom frame
        goal_position = np.array(self.goal_position[:2])
        if self.world_name.startswith("BARN"):
            robot_position = self.gazebo_sim.get_model_state().pose.position
            return robot_position.y > 11  # the special condition for BARN    
        else:
            self.goal_distance = np.sqrt(np.sum((robot_position - goal_position) ** 2))
            return self.goal_distance < 0.4

    def _get_reward(self):
        rew = self.slack_reward
        if self.step_count >= self.max_step or self._get_flip_status():
            rew = self.failure_reward
        if self._get_success():
            rew = self.success_reward
        return rew

    def _get_done(self):
        success = self._get_success()
        done = success or self.step_count >= self.max_step or self._get_flip_status()
        return done

    def _get_flip_status(self):
        robot_position = self.gazebo_sim.get_model_state().pose.position
        return robot_position.z > 0.1

    def _get_info(self):
        return dict(
            world=self.world_name,
            time=rospy.get_time() - self.start_time
        )

    def _get_local_goal(self):
        """get local goal in angle
        Returns:
            float: local goal in angle
        """
        local_goal = self.move_base.get_local_goal()
        local_goal = np.array([np.arctan2(local_goal.position.y, local_goal.position.x)])
        return local_goal

    def close(self):
        # These will make sure all the ros processes being killed
        os.system("killall -9 rosmaster")
        os.system("killall -9 gzclient")
        os.system("killall -9 gzserver")
        os.system("killall -9 roscore")

    def _set_start_goal_BARN(self, world_name):
        """Use predefined start and goal position for BARN dataset
        """
        if world_name.startswith("BARN"):
            path_dir = join(self.BASE_PATH, "worlds", "BARN", "path_files")
            world_id = int(world_name.split('_')[-1].split('.')[0])
            path = np.load(join(path_dir, 'path_%d.npy' % world_id))
            init_x, init_y = self._path_coord_to_gazebo_coord(*path[0])
            goal_x, goal_y = self._path_coord_to_gazebo_coord(*path[-1])
            init_y -= 1
            goal_x -= init_x
            goal_y -= (init_y-5) # put the goal 5 meters backward
            self.init_position = [init_x, init_y, np.pi/2]
            self.goal_position = [goal_x, goal_y, 0]

    def _path_coord_to_gazebo_coord(self, x, y):
        RADIUS = 0.075
        r_shift = -RADIUS - (30 * RADIUS * 2)
        c_shift = RADIUS + 5

        gazebo_x = x * (RADIUS * 2) + r_shift
        gazebo_y = y * (RADIUS * 2) + c_shift

        return (gazebo_x, gazebo_y)


class DWABaseLaser(DWABase):
    def __init__(self, laser_clip=4, **kwargs):
        super().__init__(**kwargs)
        self.laser_clip = laser_clip
        
        # 720 laser scan + local goal (in angle)
        self.observation_space = Box(
            low=0,
            high=laser_clip,
            shape=(721,),
            dtype=np.float32
        )

    def _get_laser_scan(self):
        """Get 720 dim laser scan
        Returns:
            np.ndarray: (720,) array of laser scan 
        """
        laser_scan = self.gazebo_sim.get_laser_scan()
        laser_scan = np.array(laser_scan.ranges)
        laser_scan[laser_scan > self.laser_clip] = self.laser_clip
        return laser_scan

    def _get_observation(self):
        # observation is the 720 dim laser scan + one local goal in angle
        laser_scan = self._get_laser_scan()
        local_goal = self._get_local_goal()
        
        laser_scan = (laser_scan - self.laser_clip/2.) / self.laser_clip # scale to (-0.5, 0.5)
        local_goal = local_goal / (2.0 * np.pi) # scale to (-0.5, 0.5)

        obs = np.concatenate([laser_scan, local_goal])

        return obs


class DWABaseCostmap(DWABase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # 720 laser scan + local goal (in angle)
        self.observation_space = Box(
            low=-1,
            high=10,
            shape=(1, 84, 84),
            dtype=np.float32
        )

    def _get_costmap(self):
        PADDING = 62
        costmap = self.move_base.get_costmap().data
        costmap = np.array(costmap, dtype=float).reshape(800, 800)
        # padding to prevent out of index
        occupancy_grid = np.zeros((800 + PADDING * 2, 800 + PADDING * 2), dtype=float)
        occupancy_grid[PADDING:800 + PADDING, PADDING:800 + PADDING] = costmap
        
        global_path = self.move_base.robot_config.global_path
        if len(global_path) > 0:
            path_index = [
                self._to_image_index(*tuple(coordinate), padding=PADDING)
                for coordinate in global_path
            ]
            for p in path_index:
                occupancy_grid[p[1], p[0]] = -100

        X, Y = self.move_base.robot_config.X, self.move_base.robot_config.Y  # robot position
        X, Y = self._to_image_index(X, Y, padding=PADDING)
        occupancy_grid = occupancy_grid[
            Y - PADDING:Y + PADDING,
            X - PADDING:X + PADDING
        ]
        obstacles_index = np.where(occupancy_grid == 100)
        path_index = np.where(occupancy_grid == -100)
        occupancy_grid[:, :] = 0.5
        occupancy_grid[obstacles_index] = 1
        occupancy_grid[path_index] = 0
        psi = self.move_base.robot_config.PSI
        occupancy_grid = self.rotate_image(occupancy_grid, psi/np.pi*180)
        w, h = occupancy_grid.shape[0], occupancy_grid.shape[1]
        occupancy_grid = occupancy_grid[w//2-42:w//2+42, h//2-42:h//2+42]
        occupancy_grid = occupancy_grid.reshape(84, 84)
        assert occupancy_grid.shape == (84, 84), "x, y, z: %d, %d, %d; X, Y: %d, %d" %(occupancy_grid.shape[0], occupancy_grid.shape[1], occupancy_grid.shape[2], X, Y)
        
        return occupancy_grid
   
    def rotate_image(self, image, angle):
        """
        Rotates an OpenCV 2 / NumPy image about it's centre by the given angle
        (in degrees). The returned image will be large enough to hold the entire
        new image, with a black background
        """

        # Get the image size
        # No that's not an error - NumPy stores image matricies backwards
        image_size = (image.shape[1], image.shape[0])
        image_center = tuple(np.array(image_size) / 2)

        # Convert the OpenCV 3x2 rotation matrix to 3x3
        rot_mat = np.vstack(
            [cv2.getRotationMatrix2D(image_center, angle, 1.0), [0, 0, 1]]
        )

        rot_mat_notranslate = np.matrix(rot_mat[0:2, 0:2])

        # Shorthand for below calcs
        image_w2 = image_size[0] * 0.5
        image_h2 = image_size[1] * 0.5

        # Obtain the rotated coordinates of the image corners
        rotated_coords = [
            (np.array([-image_w2,  image_h2]) * rot_mat_notranslate).A[0],
            (np.array([ image_w2,  image_h2]) * rot_mat_notranslate).A[0],
            (np.array([-image_w2, -image_h2]) * rot_mat_notranslate).A[0],
            (np.array([ image_w2, -image_h2]) * rot_mat_notranslate).A[0]
        ]

        # Find the size of the new image
        x_coords = [pt[0] for pt in rotated_coords]
        x_pos = [x for x in x_coords if x > 0]
        x_neg = [x for x in x_coords if x < 0]

        y_coords = [pt[1] for pt in rotated_coords]
        y_pos = [y for y in y_coords if y > 0]
        y_neg = [y for y in y_coords if y < 0]

        right_bound = max(x_pos)
        left_bound = min(x_neg)
        top_bound = max(y_pos)
        bot_bound = min(y_neg)

        new_w = int(abs(right_bound - left_bound))
        new_h = int(abs(top_bound - bot_bound))

        # We require a translation matrix to keep the image centred
        trans_mat = np.matrix([
            [1, 0, int(new_w * 0.5 - image_w2)],
            [0, 1, int(new_h * 0.5 - image_h2)],
            [0, 0, 1]
        ])

        # Compute the tranform for the combined rotation and translation
        affine_mat = (np.matrix(trans_mat) * np.matrix(rot_mat))[0:2, :]

        # Apply the transform
        result = cv2.warpAffine(
            image,
            affine_mat,
            (new_w, new_h),
            flags=cv2.INTER_LINEAR
        )

        return result

    def _to_image_index(self, x, y, padding=42):
        X, Y = int(x*20) + 400 + padding, int(y*20) + 400 + padding
        X, Y = min(799 + padding, X), min(799 + padding, Y)
        X, Y = max(padding, X), max(padding, Y)
        return X, Y

    def _get_observation(self):
        # observation is the 720 dim laser scan + one local goal in angle
        costmap = self._get_costmap()
        # for now we skip the local goal temperally
        # local_goal = local_goal / (2.0 * np.pi) # scale to (-0.5, 0.5)
        obs = costmap
        return obs

    def visual_costmap(self, costmap):
        from matplotlib import pyplot as plt
        import cv2

        costmap = (costmap * 100).astype(int).reshape(-1, 84, 84)
        costmap = np.transpose(costmap, axes=(1, 2, 0)) + 100
        costmap = np.repeat(costmap, 3, axis=2)
        plt.imshow(costmap, origin="bottomleft")
        plt.show(block=False)
        plt.pause(.5)
