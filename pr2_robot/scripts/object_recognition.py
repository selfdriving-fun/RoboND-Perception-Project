#!/usr/bin/env python

# Import modules
import numpy as np
import sklearn
import time
from sklearn.preprocessing import LabelEncoder
import pickle
from sensor_stick.srv import GetNormals
from sensor_stick.features import compute_color_histograms
from sensor_stick.features import compute_normal_histograms
from visualization_msgs.msg import Marker
from sensor_stick.marker_tools import *
from sensor_stick.msg import DetectedObjectsArray
from sensor_stick.msg import DetectedObject
# from sensor_stick.pcl_helper import *
from pcl_helper import *

import rospy
import tf
from geometry_msgs.msg import Pose
from std_msgs.msg import Float64
from std_msgs.msg import Int32
from std_msgs.msg import String
from pr2_robot.srv import *
from rospy_message_converter import message_converter
import yaml

# message creation imports
from std_msgs.msg import Int32
from std_msgs.msg import String
from geometry_msgs.msg import Pose
from geometry_msgs.msg import Point
# imports for arm movement
from sensor_msgs.msg import JointState
# import for clearing of octomap or collision cloud
from std_srvs.srv import Empty

import math
import time

DEV_FLAG = 0
OUTPUT_PCD_DIRECTORY = "output_pcd_files"

WORLD_setting = "challenge"  # set to "test1" for test1.WORLD_setting and pick_list1.yaml
                # set to "test2" for test2.WORLD_setting and pick_list2.yaml
                # set to "test3" for test3.WORLD_setting and pick_list3.yaml
                # set to "challenge" for challenge.WORLD_setting and pick_list4.yaml

ENABLE_PICK_PLACE_ROUTINE = True

if WORLD_setting == "test1":
    TEST_SCENE_NUM = 1
    WORLD = "test"
elif WORLD_setting == "test2":
    TEST_SCENE_NUM = 2
    WORLD = "test"
elif WORLD_setting == "test3":
    TEST_SCENE_NUM = 3
    WORLD = "test"
elif WORLD_setting == "challenge":
    TEST_SCENE_NUM = 4
    WORLD = "challenge"
else:
    TEST_SCENE_NUM = None
    WORLD = None

# initialize variables keeping track if all objects on the left and right of the challenge world have all been
# identified
right_objects_complete = False
left_objects_complete = False

# initialize empty lists where the challenge world will save its identified objects
global_detected_object_list_details = []
global_detected_object_labels = []


# Helper function to get surface normals
def get_normals(cloud):
    get_normals_prox = rospy.ServiceProxy('/feature_extractor/get_normals', GetNormals)
    return get_normals_prox(cloud).cluster


# Helper function to create a yaml friendly dictionary from ROS messages
def make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    yaml_dict = {}
    yaml_dict["test_scene_num"] = test_scene_num.data
    yaml_dict["arm_name"] = arm_name.data
    yaml_dict["object_name"] = object_name.data
    yaml_dict["pick_pose"] = message_converter.convert_ros_message_to_dictionary(pick_pose)
    yaml_dict["place_pose"] = message_converter.convert_ros_message_to_dictionary(place_pose)
    return yaml_dict


# Helper function that will create a dict of an identified object so that we can store it in a list
def make_object_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    object_dict = {}
    object_dict["test_scene_num"] = test_scene_num
    object_dict["arm_name"] = arm_name
    object_dict["object_name"] = object_name
    object_dict["pick_pose"] = pick_pose
    object_dict["place_pose"] = place_pose
    return object_dict


# Helper function to output to yaml file
def send_to_yaml(yaml_filename, dict_list):
    data_dict = {"object_list": dict_list}
    with open(yaml_filename, 'w') as outfile:
        yaml.dump(data_dict, outfile, default_flow_style=False)


# Function to identify if the world joint has reached its target angle
def world_joint_at_goal(goal_j1):
    joint_states = rospy.wait_for_message('/pr2/joint_states', JointState)
    # the last joint state is the world_joint
    world_joint_pos = joint_states.position[-1]
    tolerance = .05
    result = abs(world_joint_pos - goal_j1) <= abs(tolerance)
    return result


# Function to move the world joint to target angle
def move_world_joint(goal_j1):
    move_complete = False

    while move_complete is False:
        world_joint_controller_pub.publish(goal_j1)

        if world_joint_at_goal(goal_j1):
            print("move complete to " + str(goal_j1) + " complete")
            move_complete = True

    return move_complete


def passthrough_filter_challenge_world(pcl_cloud):
    # passthrough filter ranges to remove tables
    # bottom area 0 - 0.45
    # middle area 0.55(left side) 0.551 (right side) - 0.775
    # top area 0.825 (left side) 0.8251 (right side) - 1.0

    # **************** START filter bottom layer *******************
    passthrough_filter_bottom = pcl_cloud.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_filter_bottom.set_filter_field_name(filter_axis)
    # .00001 for bottom axis min so that we can omit the floor
    bottom_axis_min_z = 0.0001
    bottom_axis_max_z = 0.45
    passthrough_filter_bottom.set_filter_limits(bottom_axis_min_z, bottom_axis_max_z)

    # Finally use the filter function to obtain the resultant point cloud.
    cloud_filtered_z_bottom = passthrough_filter_bottom.filter()

    # Once we've obtained the area beneath the bottom table, we will now omit the table stand to the left
    passthrough_filter_y_bottom_left = cloud_filtered_z_bottom.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'y'
    passthrough_filter_y_bottom_left.set_filter_field_name(filter_axis)
    bottom_axis_min_y_left = 1
    bottom_axis_max_y_left = 20
    passthrough_filter_y_bottom_left.set_filter_limits(bottom_axis_min_y_left, bottom_axis_max_y_left)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_bottom_left = passthrough_filter_y_bottom_left.filter()

    # We will now omit the table stand to the right
    passthrough_filter_y_bottom_right = cloud_filtered_z_bottom.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'y'
    passthrough_filter_y_bottom_right.set_filter_field_name(filter_axis)
    bottom_axis_min_y_right = -20
    bottom_axis_max_y_right = -1
    passthrough_filter_y_bottom_right.set_filter_limits(bottom_axis_min_y_right, bottom_axis_max_y_right)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_bottom_right = passthrough_filter_y_bottom_right.filter()
    # **************** END filter bottom layer *******************

    # **************** START filter middle layer *******************
    # Get the z layer between the bottom table and the top table
    passthrough_filter_z_middle = pcl_cloud.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_filter_z_middle.set_filter_field_name(filter_axis)
    middle_axis_min = 0.558  # 0.551 works for left and right, but 0.558 works for front also
    middle_axis_max = 0.775
    passthrough_filter_z_middle.set_filter_limits(middle_axis_min, middle_axis_max)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_z_middle = passthrough_filter_z_middle.filter()

    # Omit the table stand of the table in front
    passthrough_filter_x_middle = cloud_filtered_z_middle.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'x'
    passthrough_filter_x_middle.set_filter_field_name(filter_axis)
    middle_axis_min = -20
    middle_axis_max = .7
    passthrough_filter_x_middle.set_filter_limits(middle_axis_min, middle_axis_max)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_x_middle = passthrough_filter_x_middle.filter()

    # Exclude everything after the object area in left middle layer including the table stem
    passthrough_filter_y_middle_left = cloud_filtered_x_middle.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'y'
    passthrough_filter_y_middle_left.set_filter_field_name(filter_axis)
    middle_axis_min_y_left = 0
    middle_axis_max_y_left = 0.855
    passthrough_filter_y_middle_left.set_filter_limits(middle_axis_min_y_left, middle_axis_max_y_left)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_middle_left = passthrough_filter_y_middle_left.filter()

    # Exclude everything after the object area in right middle layer including the table stem
    passthrough_filter_y_middle_right = cloud_filtered_x_middle.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'y'
    passthrough_filter_y_middle_right.set_filter_field_name(filter_axis)
    middle_axis_min_y_right = -0.855
    middle_axis_max_y_right = 0
    passthrough_filter_y_middle_right.set_filter_limits(middle_axis_min_y_right, middle_axis_max_y_right)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_middle_right = passthrough_filter_y_middle_right.filter()
    # **************** END filter middle layer *******************

    # **************** START filter top layer *******************
    # Get the layer above the top table
    passthrough_filter_top = pcl_cloud.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_filter_top.set_filter_field_name(filter_axis)
    # top_axis_min = .6101
    # .6 for test world .5 or 0 for challenge world
    top_axis_min = 0.826
    top_axis_max = 1.0
    passthrough_filter_top.set_filter_limits(top_axis_min, top_axis_max)

    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_z_top = passthrough_filter_top.filter()
    # **************** END filter top layer *******************

    # convert to arrays,then to lists, to enable combination later on
    cloud_filtered_bottom_list = cloud_filtered_bottom_left.to_array().tolist() + cloud_filtered_bottom_right.to_array().tolist()
    cloud_filtered_middle_list = cloud_filtered_middle_left.to_array().tolist() + cloud_filtered_middle_right.to_array().tolist()
    cloud_filtered_z_top_list = cloud_filtered_z_top.to_array().tolist()

    # We have chosen not to include the bottom layer to reduce comlexity. This is because the object recognition
    # pipeline fails to identify the "create" objects roaming around the challenge world.
    combined_passthrough_filtered_list = cloud_filtered_middle_list + cloud_filtered_z_top_list

    # Reconvert into PointCloud format
    filtered_cloud = pcl.PointCloud_PointXYZRGB()
    filtered_cloud.from_list(combined_passthrough_filtered_list)

    return filtered_cloud


def passthrough_filter_challenge_world_extract_table(pcl_cloud):
    # This filter is meant to get the PointClouds of the tables in the challenge world
    # bottom table filter
    passthrough_filter_bottom_table = pcl_cloud.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_filter_bottom_table.set_filter_field_name(filter_axis)
    bottom_table_axis_min = 0.46
    bottom_table_axis_max = 0.557
    passthrough_filter_bottom_table.set_filter_limits(bottom_table_axis_min, bottom_table_axis_max)
    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_z_bottom_table = passthrough_filter_bottom_table.filter()

    # top table filter
    passthrough_filter_top_table = pcl_cloud.make_passthrough_filter()
    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_filter_top_table.set_filter_field_name(filter_axis)
    top_table_axis_min = 0.776
    top_table_axis_max = 0.86
    passthrough_filter_top_table.set_filter_limits(top_table_axis_min, top_table_axis_max)
    # Use the filter function to obtain the resultant point cloud.
    cloud_filtered_z_top_table = passthrough_filter_top_table.filter()

    cloud_filtered_z_bottom_table_list = cloud_filtered_z_bottom_table.to_array().tolist()
    cloud_filtered_z_top_table_list = cloud_filtered_z_top_table.to_array().tolist()

    combined_passthrough_filtered_list = cloud_filtered_z_bottom_table_list + cloud_filtered_z_top_table_list

    filtered_table_cloud = pcl.PointCloud_PointXYZRGB()
    filtered_table_cloud.from_list(combined_passthrough_filtered_list)

    return filtered_table_cloud


def passthrough_filter_test_world(pcl_cloud):
    # PassThrough Filter for z axis to remove table
    passthrough_z = pcl_cloud.make_passthrough_filter()

    # Assign axis and range to the passthrough filter object.
    filter_axis = 'z'
    passthrough_z.set_filter_field_name(filter_axis)
    # axis_min = .6101
    # .6 for test world .5 or 0 for challenge world
    axis_min = 0.6
    axis_max = 1.0
    passthrough_z.set_filter_limits(axis_min, axis_max)

    # Finally use the filter function to obtain the resultant point cloud.
    cloud_filtered_z = passthrough_z.filter()

    # Omit dropbox areas
    passthrough_dropbox_x = cloud_filtered_z.make_passthrough_filter()

    # Assign axis and range to the passthrough filter object.
    filter_axis = 'x'
    passthrough_dropbox_x.set_filter_field_name(filter_axis)
    # to obtain the dropbox x areas only, axis_min = -1.0, axis_max = .339
    # to omit dropboxes and get the table areas only, axis_min = .339 and axis_max = 1
    axis_min = 0.339
    axis_max = 1
    passthrough_dropbox_x.set_filter_limits(axis_min, axis_max)

    filtered_cloud = passthrough_dropbox_x.filter()

    return filtered_cloud


def compute_place_pose_offsets(item_number_for_group, place_position_horizontal_coefficient=0.08,
                               place_position_vertical_coefficient=0.1):
    # compute horizontal adjustment
    if (item_number_for_group % 3) == 1:
        horizontal_adjustment = - (item_number_for_group * place_position_horizontal_coefficient)
    elif (item_number_for_group % 3) == 2:
        horizontal_adjustment = 0
    elif (item_number_for_group % 3) == 0:
        horizontal_adjustment = (item_number_for_group * place_position_horizontal_coefficient)
    else:
        horizontal_adjustment = 0

    # compute for vertical adjustment
    layer = math.ceil(item_number_for_group / 3.0)

    vertical_adjustment = -(layer * place_position_vertical_coefficient)
    if vertical_adjustment <= -1:
        vertical_adjustment = -0.9

    return horizontal_adjustment, vertical_adjustment


def compute_challenge_world_place_pose(item_number_for_group, y_coefficient=0.3, reverse_y=False):

    # this first object for the arm is placed on these coordinates
    if item_number_for_group == 1:
        # placed on the top front table
        xpos = 0.8
        # placed a bit off from the center  of the table, towards the left
        ypos = 1 * y_coefficient
        # placed on the top front table
        zpos = 0.9
    # this second object for the arm is placed on these coordinates
    elif item_number_for_group == 2:
        # placed on the top front table
        xpos = 0.8
        # placed a bit off from the center  of the table, towards the left
        ypos = 2 * y_coefficient
        # placed on the top front table
        zpos = 0.9
    elif item_number_for_group == 3:
        # placed on the bottom front table
        xpos = 0.45
        # placed a bit off from the center  of the table, towards the left
        ypos = 1 * y_coefficient
        # placed on the bottom front table
        zpos = 0.6
    elif item_number_for_group == 4:
        # placed on the bottom front table
        xpos = 0.45
        # placed a bit off from the center  of the table, towards the left
        ypos = 2 * y_coefficient
        # placed on the bottom front table
        zpos = 0.6
    else:
        xpos = 0
        ypos = 0
        zpos = 0

    # if we are using the right arm, and we want to line the objects to the right, we reverse the y value
    if reverse_y:
        ypos = -ypos

    return xpos, ypos, zpos


# Callback function for your Point Cloud Subscriber
def pcl_callback(pcl_msg):
    # Invoke global variables for challenge world
    global right_objects_complete
    global left_objects_complete

    global global_detected_object_list_details
    global global_detected_object_labels

    # If we are dealing with "challenge.world", then we instruct the robot to turn right and identify the objects on
    # the right, and then turn left to identify the objects on the left
    if WORLD == "challenge":
        # If not all of the objects on the right have been successfully identified we turn to the right
        if not right_objects_complete:
            print("turning right")
            # Check if the move has been completed. if yes, set the current side to "right"
            move_complete = move_world_joint(-np.math.pi / 2)
            if move_complete:
                current_side = "right"
            else:
                current_side = "turning_right"
        # If not all of the objects on the left have been successfully identified we turn to the left
        elif not left_objects_complete:
            print("turning left")
            # Check if the move has been completed. if yes, set the current side to "left"
            move_complete = move_world_joint(np.math.pi / 2)
            if move_complete:
                current_side = "left"
            else:
                current_side = "turning_left"
        # If done with both sides, return to the original front facing orientation
        else:
            print("returning to 0 orientation")
            move_complete = move_world_joint(0)
            if move_complete:
                current_side = "front"
            else:
                current_side = "turning_to_the_front"

    # if DEV_FLAG is 0, no conversion to pcl necessary, otherwise, convert ROS to PCL
    if DEV_FLAG == 1:
        cloud = pcl_msg
    else:
        cloud = ros_to_pcl(pcl_msg)

    # Exercise-2 TODOs: segment and cluster the objects
    # Remove noise from the point cloud
    outlier_filter = cloud.make_statistical_outlier_filter()

    # Set the number of neighboring points to analyze for any given point
    outlier_filter.set_mean_k(10)

    # Any point with a mean distance larger than global (mean distance+x*std_dev) will be considered outlier
    outlier_filter.set_std_dev_mul_thresh(1)

    # Perform the noise filtration
    cloud = outlier_filter.filter()
    # pcl.save(cloud, OUTPUT_PCD_DIRECTORY + "/noise_reduced.pcd")
    print ("noise filtering done")

    # Voxel Grid Downsampling
    vox = cloud.make_voxel_grid_filter()
    # We've set the leaf side to a lower value for greater detail
    LEAF_SIZE = .003

    # Set the voxel (or leaf) size
    vox.set_leaf_size(LEAF_SIZE, LEAF_SIZE, LEAF_SIZE)

    # Call the filter function to obtain the resultant downsampled point cloud
    cloud_filtered = vox.filter()

    # pcl.save(cloud_filtered, OUTPUT_PCD_DIRECTORY + "/voxel_downsampled.pcd")
    print("voxel downsampling done")

    # conduct passthrough filtering
    if WORLD == "challenge":
        # if the world is the challenge world perform passthrough filtering for challenge worlds
        cloud_objects = passthrough_filter_challenge_world(cloud_filtered)
        cloud_table = passthrough_filter_challenge_world_extract_table(cloud_filtered)
        # pcl.save(cloud_objects, OUTPUT_PCD_DIRECTORY + "/passthrough_filtered.pcd")
        print("challenge world passthrough filtering done")

        # No RANSAC segmentation for challenge world, since all tables are passthrough filtered
        # further RANSAC segmentation segments away object surfaces i.e book surfaces may be removed

    elif WORLD == "test":
        # if the world is the test world perform passthrough filtering for test worlds
        cloud_filtered = passthrough_filter_test_world(cloud_filtered)

        # pcl.save(cloud_filtered, OUTPUT_PCD_DIRECTORY + "/passthrough_filtered.pcd")
        print("test world passthrough filtering done")

        # RANSAC Plane Segmentation
        seg = cloud_filtered.make_segmenter()

        # Set the model you wish to fit
        seg.set_model_type(pcl.SACMODEL_PLANE)
        seg.set_method_type(pcl.SAC_RANSAC)

        # Max distance for a point to be considered fitting the model
        max_distance = .003
        seg.set_distance_threshold(max_distance)

        # Call the segment function to obtain set of inlier indices and model coefficients
        inliers, coefficients = seg.segment()

        # Extract inliers and outliers
        # Extract inliers - models that fit the model (plane)
        cloud_table = cloud_filtered.extract(inliers, negative=False)
        # Extract outliers - models that do not fit the model (non-planes)
        cloud_objects = cloud_filtered.extract(inliers, negative=True)

        # pcl.save(cloud_table, OUTPUT_PCD_DIRECTORY + "/cloud_table.pcd")
        # pcl.save(cloud_objects, OUTPUT_PCD_DIRECTORY + "/cloud_objects.pcd")
        print("RANSAC segmentation done")

    else:
        # no passthrough filtering for cloud
        cloud_objects = cloud_filtered
        cloud_table = None
        print("No passthrough filtering and RANSAC segmentation performed")

    # Euclidean Clustering
    white_cloud = XYZRGB_to_XYZ(cloud_objects)
    tree = white_cloud.make_kdtree()

    # Create Cluster-Mask Point Cloud to visualize each cluster separately
    # Create a cluster extraction object
    ec = white_cloud.make_EuclideanClusterExtraction()
    # Set tolerances for distance threshold
    # as well as minimum and maximum cluster size (in points)
    ec.set_ClusterTolerance(0.015)
    ec.set_MinClusterSize(150)
    ec.set_MaxClusterSize(50000)
    # Search the k-d tree for clusters
    ec.set_SearchMethod(tree)
    # Extract indices for each of the discovered clusters
    cluster_indices = ec.Extract()

    cluster_color = get_color_list(len(cluster_indices))

    color_cluster_point_list = []

    for j, indices in enumerate(cluster_indices):
        for i, index in enumerate(indices):
            color_cluster_point_list.append([white_cloud[index][0],
                                             white_cloud[index][1],
                                             white_cloud[index][2],
                                             rgb_to_float(cluster_color[j])])

    # Create new cloud containing all clusters, each with unique color
    cluster_cloud = pcl.PointCloud_PointXYZRGB()
    cluster_cloud.from_list(color_cluster_point_list)

    # pcl.save(cluster_cloud, OUTPUT_PCD_DIRECTORY + "/cluster_cloud.pcd")
    print("Euclidean clustering done")

    # Convert PCL data to ROS messages
    ros_cloud_objects = pcl_to_ros(cluster_cloud)
    if cloud_table:
        ros_cloud_table = pcl_to_ros(cloud_table)

    # Publish ROS messages
    pcl_objects_pub.publish(ros_cloud_objects)
    if cloud_table:
        pcl_table_pub.publish(ros_cloud_table)

    # Exercise-3 TODOs: identify the objects

    # initialize empty lists that will hold labels and detected objects, as well as unidentified object counter
    detected_objects_labels = []
    detected_objects = []
    uncertain_clusters = 0

    for index, pts_list in enumerate(cluster_indices):
        # Grab the points for the cluster
        pcl_cluster = cloud_objects.extract(pts_list)
        # Convert the cluster from pcl to ROS using helper function
        ros_cluster = pcl_to_ros(pcl_cluster)

        # Extract histogram features
        chists = compute_color_histograms(ros_cluster, using_hsv=True)
        normals = get_normals(ros_cluster)
        nhists = compute_normal_histograms(normals)
        feature = np.concatenate((chists, nhists))

        # Make the prediction, retrieve the label for the result
        # and add it to detected_objects_labels list

        # This will return an array with the probabilities of each choice
        prediction_confidence_list = clf.predict_proba(scaler.transform(feature.reshape(1, -1)))

        # This will return the index with the highest probability
        prediction = clf.predict(scaler.transform(feature.reshape(1, -1)))
        print("prediction ", type(prediction), prediction)

        # This will return the confidence value in of the prediction
        prediction_confidence = prediction_confidence_list[0][prediction]

        # If the prediction_confidence is greater than 60%, proceed, else, add 1 to unidentified cluster counter
        if prediction_confidence > 0.60:
            label = encoder.inverse_transform(prediction)[0]
        else:
            label = encoder.inverse_transform(prediction)[0]
            # add a count to the unidentified clusters
            uncertain_clusters += 1
            print("not sure if this is really a " + label)

        # Add the label to the list of detected object's labels
        detected_objects_labels.append(label)

        # Publish a label into RViz
        label_pos = list(white_cloud[pts_list[0]])
        label_pos[2] += .4
        print("label", label, prediction_confidence)
        object_markers_pub.publish(make_label(label, label_pos, index))

        # Add the detected object to the list of detected objects.
        do = DetectedObject()
        do.label = label
        do.cloud = ros_cluster
        detected_objects.append(do)

    # If the world is a challenge world, all clusters have been identified,
    # and we're not reidentifying the same set of items
    # then raise the flags that identification is complete
    if WORLD == "challenge":
        if detected_objects and (uncertain_clusters <= 1) and (
            set(global_detected_object_labels) != set(detected_objects_labels)):
            print("current side ", current_side)
            if current_side == "right":
                right_objects_complete = True
                global_detected_object_list_details.extend(detected_objects)
                global_detected_object_labels = detected_objects_labels
                print("All objects on the right labeled")
            elif current_side == "left":
                left_objects_complete = True
                global_detected_object_list_details.extend(detected_objects)
                global_detected_object_labels.extend(detected_objects_labels)
                print("All objects on the left labeled")

        if right_objects_complete and left_objects_complete:
            detected_objects = global_detected_object_list_details

    # Publish the list of detected objects
    rospy.loginfo('Detected {} objects: {}'.format(len(detected_objects_labels), detected_objects_labels))

    # yaml publishing code
    # object_list_param is an ordered list of dicts
    object_list_param = rospy.get_param('/object_list')
    # object_list_param = [{'group': 'red', 'name': 'sticky_notes'}, {'group': 'red', 'name': 'book'}, {'group': 'green', 'name': 'snacks'}, {'group': 'green', 'name': 'biscuits'}, {'group': 'red', 'name': 'eraser'}, {'group': 'green', 'name': 'soap2'}, {'group': 'green', 'name': 'soap'}, {'group': 'red', 'name': 'glue'}]

    dropbox = rospy.get_param('/dropbox')
    # dropbox = [{'position': [0, 0.71, 0.605], 'group': 'red', 'name': 'left'}, {'position': [0, -0.71, 0.605], 'group': 'green', 'name': 'right'}]

    # Code to identify how many items have already been placed in each dropbox
    # Count the number of items destined for each dropbox
    dropbox1_members = []
    dropbox2_members = []
    for object_item in object_list_param:
        if object_item["group"] == dropbox[0]["group"]:
            dropbox1_members.append(object_item["name"])
        elif object_item["group"] == dropbox[1]["group"]:
            dropbox2_members.append(object_item["name"])

    dropbox1_intended_count = len(dropbox1_members)
    dropbox2_intended_count = len(dropbox2_members)

    # Count the number of detected objects on the table(s) destined for each dropbox, maybe do this for challenge world
    # only once we have completely identified all the objects on the scene
    dropbox1_unpicked = []
    dropbox2_unpicked = []
    for detected_object in detected_objects:
        # check what group each detected object is meant to be in
        if detected_object.label in dropbox1_members:
            dropbox1_unpicked.append(detected_object.label)
        elif detected_object.label in dropbox2_members:
            dropbox2_unpicked.append(detected_object.label)

    dropbox1_unpicked_count = len(dropbox1_unpicked)
    dropbox2_unpicked_count = len(dropbox2_unpicked)

    # Infer the number of already picked objects in each box by subtracting the items on the table from those on the
    # pick list
    dropbox1_picked_count = dropbox1_intended_count - dropbox1_unpicked_count
    dropbox2_picked_count = dropbox2_intended_count - dropbox2_unpicked_count

    labels = []
    centroids = []  # to be list of tuples (x, y, z)
    dict_list = []
    object_dict_items = {}

    # The coefficients are the multiples with which we determine the x and y place poses of the objects
    place_position_vertical_coefficient = .1
    place_position_horizontal_coefficient = .08

    # We now proceed to determine the properties (centroid, label, pick pose, etc., of each identified cluster)
    # We only work with items that are found in the pick lists
    for i in range(len(object_list_param)):
        object_name = object_list_param[i]['name']
        object_group = object_list_param[i]['group']

        for object in detected_objects:
            # If one of the detected objects matches the item in the pick list, we proceed to process it
            if object.label == object_name:
                labels.append(object.label)
                points_arr = ros_to_pcl(object.cloud).to_array()

                computed_centroid = np.mean(points_arr, axis=0)[:3]

                centroids.append(computed_centroid)

                test_scene_num = Int32()
                test_scene_num.data = TEST_SCENE_NUM

                # Initialize a variable
                object_name = String()
                # Populate the data field
                object_name.data = str(object.label)

                # prepare pick_pose
                position = Point()
                position.x = float(computed_centroid[0])
                position.y = float(computed_centroid[1])
                position.z = float(computed_centroid[2])

                pick_pose = Pose()
                pick_pose.position = position

                # prepare place_pose and arm_name
                place_pose = Pose()
                arm_name = String()
                print("label", object.label)
                if object_group == dropbox[0]['group']:
                    # Derive what the nth value of the object is in the box by adding 1 to the objects already picked
                    # that are in the box. We do this for this item and all the items following it
                    dropbox1_picked_count += 1
                    print("first", dropbox1_picked_count)

                    if WORLD == "test":
                        # compute horizontal and vertical adjustment for place pose
                        horizontal_adjustment, vertical_adjustment = compute_place_pose_offsets(dropbox1_picked_count,
                                                                                                place_position_horizontal_coefficient,
                                                                                                place_position_vertical_coefficient)

                        place_position = Point()

                        place_position.x = dropbox[0]['position'][0] + vertical_adjustment
                        place_position.y = dropbox[0]['position'][1] + horizontal_adjustment
                        place_position.z = dropbox[0]['position'][2]
                    elif WORLD == "challenge":
                        xpos, ypos, zpos = compute_challenge_world_place_pose(dropbox1_picked_count)

                        place_position = Point()

                        place_position.x = xpos
                        place_position.y = ypos
                        place_position.z = zpos

                    place_pose.position = place_position

                    arm_name.data = dropbox[0]['name']
                elif object_group == dropbox[1]['group']:
                    dropbox2_picked_count += 1

                    print("second", dropbox2_picked_count)

                    if WORLD == "test":
                        # compute horizontal and vertical adjustment for place pose
                        horizontal_adjustment, vertical_adjustment = compute_place_pose_offsets(dropbox2_picked_count,
                                                                                                place_position_horizontal_coefficient,
                                                                                                place_position_vertical_coefficient)

                        place_position = Point()

                        place_position.x = dropbox[1]['position'][0] + vertical_adjustment
                        place_position.y = dropbox[1]['position'][1] + horizontal_adjustment
                        place_position.z = dropbox[1]['position'][2]
                    elif WORLD == "challenge":
                        xpos, ypos, zpos = compute_challenge_world_place_pose(dropbox2_picked_count, reverse_y=True)

                        place_position = Point()

                        place_position.x = xpos
                        place_position.y = ypos
                        place_position.z = zpos

                    place_pose.position = place_position

                    arm_name.data = dropbox[1]['name']
                else:
                    raise "object is not categorized"

                object_properties_dict = make_yaml_dict(test_scene_num, arm_name, object_name,
                                                        pick_pose, place_pose)
                dict_list.append(object_properties_dict)

                object_dict = make_object_dict(test_scene_num, arm_name, object_name,
                                               pick_pose, place_pose)
                object_dict_items[object_name.data] = object_dict

                continue

    # If items from the pick_list is present, generate the yaml file
    if dict_list:
        if WORLD == 'test':
            send_to_yaml("./output_" + str(test_scene_num.data) + ".yaml", dict_list)
            print("TEST WORLD yaml messages generated and saved to output_" + str(test_scene_num.data) + ".yaml")
        elif (WORLD == 'challenge') and right_objects_complete and left_objects_complete:
            send_to_yaml("./output_" + str(test_scene_num.data) + ".yaml", dict_list)
            print("CHALLENGE WORLD yaml messages generated and saved to output_" + str(test_scene_num.data) + ".yaml")

    # Perform pick_place_routine
    # If the pick place routine is enabled, and the world is a test world
    if ENABLE_PICK_PLACE_ROUTINE:
        # Assign which detected object list to work on
        if WORLD == "test":
            detected_objects_to_pick = detected_objects
        elif (WORLD == "challenge") and (right_objects_complete and left_objects_complete) and (current_side == "front"):
            detected_objects_to_pick = global_detected_object_list_details
        else:
            detected_objects_to_pick = []

        # Generate the pick list
        pick_list_items = [object_item['name'] for object_item in object_list_param]
        # Get the index of the label of the detected object in the object_list_param
        # Determine if a first object exists in detected_objects, and check if in the pick list
        if detected_objects_to_pick and (detected_objects_to_pick[0].label in pick_list_items):
            # Assign the contents of the correct object ot pick
            object_to_pick = object_dict_items[detected_objects_to_pick[0].label]
            # If yes, generate collision map, start with the table
            collision_map_pcl_list_form = cloud_table.to_array().tolist()
            # Add all other objects in the list
            for other_item in detected_objects_to_pick[1:]:
                collision_map_pcl_list_form += ros_to_pcl(other_item.cloud).to_array().tolist()
            # Publish collision map
            collision_map_pcl = pcl.PointCloud_PointXYZRGB()
            collision_map_pcl.from_list(collision_map_pcl_list_form)
            collision_map_ros = pcl_to_ros(collision_map_pcl)
            collidable_objects_pub.publish(collision_map_ros)

            # Proceed to pick the object
            print("picking up " + object_to_pick["object_name"].data)
            print("pick pose " + str(object_to_pick["pick_pose"]))
            print("place pose " + str(object_to_pick["place_pose"]))
            grasp_list = rospy.get_param('/grasp_list')
            print("grasp_list " + str(grasp_list))
            rospy.wait_for_service('pick_place_routine')

            try:
                pick_place_routine = rospy.ServiceProxy('pick_place_routine', PickPlace)

                resp = pick_place_routine(object_to_pick["test_scene_num"], object_to_pick["object_name"],
                                          object_to_pick["arm_name"], object_to_pick["pick_pose"],
                                          object_to_pick["place_pose"])

                print ("Response: ", resp.success)

            except rospy.ServiceException, e:
                print "Service call failed: %s" % e

            object_to_pick = None

        # If no detected objects, do nothing
        else:
            pass

    # Clear the octomap/collision map once we are done
    rospy.wait_for_service('clear_octomap')

    try:
        # https://answers.ros.org/question/12793/rospy-calling-clear-service-programatically/?answer=18877#post-id-18877
        clear_collision_map_proxy = rospy.ServiceProxy('clear_octomap', Empty)
        resp = clear_collision_map_proxy()

        print ("Response: ", resp)

    except rospy.ServiceException, e:
        print "Service call failed: %s" % e

    # save the cloud image to disk for potential diagnostic purposes
    pcl.save(ros_to_pcl(pcl_msg), "new_cloud.pcd")
    print("saved image of scene")


if __name__ == '__main__':
    # ROS node initialization
    rospy.init_node('clustering', anonymous=True)

    # world_joint_publisher
    world_joint_controller_pub = rospy.Publisher("/pr2/world_joint_controller/command", Float64, queue_size=20)

    # Create Publishers
    pcl_objects_pub = rospy.Publisher("/pcl_objects", PointCloud2, queue_size=1)
    pcl_table_pub = rospy.Publisher("/pcl_table", PointCloud2, queue_size=1)

    object_markers_pub = rospy.Publisher("/object_markers", Marker, queue_size=1)
    detected_objects_pub = rospy.Publisher("/detected_objects", PointCloud2, queue_size=1)

    # collidable object publisher
    collidable_objects_pub = rospy.Publisher("/pr2/3D_map/points", PointCloud2, queue_size=20)

    # Create Subscribers
    pcl_sub = rospy.Subscriber("/pr2/world/points", pc2.PointCloud2, pcl_callback, queue_size=1)

    # Initialize color_list
    get_color_list.color_list = []

    # Load Model From disk
    model = pickle.load(open('object_recognition_models/model.sav', 'rb'))
    clf = model['classifier']
    encoder = LabelEncoder()
    encoder.classes_ = model['classes']
    scaler = model['scaler']

    # Spin while node is not shutdown
    while not rospy.is_shutdown():
        rospy.spin()
