import numpy as np
import os
import PIL
import pycolmap
import torch
from pathlib import Path
from tqdm import tqdm
import logging
import PIL.Image
import limap.base as _base
import limap.pointsfm as _psfm
import limap.util.io as limapio

from hloc import (extract_features, localize_sfm, match_features, pairs_from_covisibility, triangulation,
                  pairs_from_poses)
from hloc.utils.read_write_model import read_model, write_model, Camera, Image, Point3D, rotmat2qvec
from hloc.pipelines.Cambridge.utils import create_query_list_with_intrinsics, evaluate
from hloc.utils.parsers import *

###############################################################################
# The following utils functions are taken/modified from hloc.pipelines.ScanNet
###############################################################################

logger = logging.getLogger('hloc')

def create_reference_sfm(full_model, ref_model, blacklist=None, ext='.bin'):
    '''Create a new COLMAP model with only training images.'''
    logger.info('Creating the reference model.')
    ref_model.mkdir(exist_ok=True)
    cameras, images, points3D = read_model(full_model, ext)

    if blacklist is not None:
        with open(blacklist, 'r') as f:
            blacklist = f.read().rstrip().split('\n')

    train_ids = []
    test_ids = []
    images_ref = dict()
    for id_, image in images.items():
        if blacklist and image.name in blacklist:
            test_ids.append(id_)
            continue
        train_ids.append(id_)
        images_ref[id_] = image

    points3D_ref = dict()
    for id_, point3D in points3D.items():
        ref_ids = [i for i in point3D.image_ids if i in images_ref]
        if len(ref_ids) == 0:
            continue
        points3D_ref[id_] = point3D._replace(image_ids=np.array(ref_ids))

    write_model(cameras, images_ref, points3D_ref, ref_model, '.bin')
    logger.info(f'Kept {len(images_ref)} images out of {len(images)}.')
    return train_ids, test_ids


def create_reference_sfm_from_ScanNetDatset(data_path, ref_model, ext=".bin"):
    '''
    Create a new COLMAP model with known camera poses and intrinsics,
    without any points3D as well as 2D points.
    '''
    ref_model.mkdir(exist_ok=True)
    cameras = dict()
    images = dict()
    points3D = dict()
    img_color_path = data_path / "color"
    intrinsic_path = data_path / "intrinsic"
    pose_path = data_path / "pose"
    file_list = os.listdir(img_color_path)
    
    # read camera intrinsics
    with open(intrinsic_path / "intrinsic_color.txt", "r") as f:
        intrinsic = f.read().rstrip().split("\n")
        intrinsic = [list(map(float, x.split())) for x in intrinsic]
        intrinsic = np.array(intrinsic)
    cameras[0] = Camera(
                    id=0, model="PINHOLE", width=1296, height=968, params=np.array((intrinsic[0,0], 
                                                                                   intrinsic[1,1], intrinsic[0,2], intrinsic[1,2]))
                )
    imgs_nopose = 0
    for file in file_list:
        image_id = int(file.split(".")[0])
        tmp_path = pose_path / f"{image_id}.txt"
        with open(tmp_path, "r") as f:
            pose = f.read().rstrip().split("\n")
            pose = [list(map(float, x.split())) for x in pose]
            pose = np.array(pose)
        if np.isinf(pose).any():
            imgs_nopose += 1
            continue
        pose = np.linalg.inv(pose)
        t = pose[:3, 3]
        R = pose[:3, :3]
        q = rotmat2qvec(R)
        images[image_id] = Image(
            id=image_id, qvec=q, tvec=t, camera_id=0, name=file, xys=[], point3D_ids=[]
        )
    print("[Warning] {} images have no pose.".format(imgs_nopose))
    write_model(cameras, images, points3D, ref_model, ".bin")


def scene_coordinates(p2D, R_w2c, t_w2c, depth, camera):
    assert len(depth) == len(p2D)
    K = np.array([[camera.params[0], 0, camera.params[2]],
                  [0, camera.params[1], camera.params[3]],
                  [0, 0, 1]])
    p2D_homogeneous = np.concatenate([p2D, np.ones((p2D.shape[0], 1))], axis=1)
    p2D_normalized = np.linalg.inv(K) @ p2D_homogeneous.T
    p3D_c = np.multiply(p2D_normalized.T, depth[:, None])
    p3D_w = (p3D_c - t_w2c) @ R_w2c
    return p3D_w


def interpolate_depth(depth, kp, image_size):
    
    # Resize depth image to match the size of the actual image
    depth_tensor = torch.from_numpy(depth).unsqueeze(0).unsqueeze(0)
    depth_tensor = torch.nn.functional.interpolate(depth_tensor, size=image_size, mode='bilinear', align_corners=False)
    depth = depth_tensor.squeeze().numpy()
    
    h, w = depth.shape
    kp = kp / np.array([[w - 1, h - 1]]) * 2 - 1
    assert np.all(kp > -1) and np.all(kp < 1)
    
    kp = torch.from_numpy(kp).unsqueeze(0).unsqueeze(0)
    grid_sample = torch.nn.functional.grid_sample

    # To maximize the number of points that have depth:
    # do bilinear interpolation first and then nearest for the remaining points
    interp_lin = grid_sample(depth_tensor, kp, align_corners=True, mode="bilinear")[0, :, 0]
    interp_nn = torch.nn.functional.grid_sample(
        depth_tensor, kp, align_corners=True, mode="nearest"
    )[0, :, 0]
    interp = torch.where(torch.isnan(interp_lin), interp_nn, interp_lin)
    valid = ~torch.any(torch.isnan(interp), 0)

    interp_depth = interp.T.numpy().flatten()
    valid = valid.numpy()
    return interp_depth, valid


def image_path_to_rendered_depth_path(image_name):
    name = image_name.replace("jpg", "png")
    return name


def project_to_image(p3D, R, t, camera, eps: float = 1e-4, pad: int = 1):
    p3D = (p3D @ R.T) + t
    visible = p3D[:, -1] >= eps  # keep points in front of the camera

    K = np.array([[camera.params[0], 0, camera.params[2]],
                  [0, camera.params[1], camera.params[3]],
                  [0, 0, 1]])
    p2D_homogeneous = p3D[:, :-1] / p3D[:, -1:].clip(min=eps)
    p2D_homogeneous = np.concatenate([p2D_homogeneous, np.ones((p2D_homogeneous.shape[0], 1))], axis=1)
    p2D = p2D_homogeneous @ K.T

    size = np.array([camera.width - pad - 1, camera.height - pad - 1])
    valid = np.all((p2D[:, :2] >= pad) & (p2D[:, :2] <= size), -1)
    valid &= visible
    return p2D[valid, :2], valid


def correct_sfm_with_gt_depth(sfm_path, depth_folder_path, output_path):
    cameras, images, points3D = read_model(sfm_path)
    logger.info("Correcting sfm using depth...")
    for imgid, img in tqdm(images.items()):
        image_name = img.name
        depth_name = image_path_to_rendered_depth_path(image_name)

        depth = PIL.Image.open(Path(depth_folder_path) / depth_name)
        depth = np.array(depth).astype('float64')
        depth = depth/1000.  # mm to meter
        depth[(depth == 0.0) | (depth > 1000.0)] = np.nan

        R_w2c, t_w2c = img.qvec2rotmat(), img.tvec
        camera = cameras[img.camera_id]
        p3D_ids = img.point3D_ids
        try:
            p3Ds = np.stack([points3D[i].xyz for i in p3D_ids[p3D_ids != -1]], 0)
        except:
            print(f"[Warning] Image {image_name} has no 3D points.")
            continue
            # import pdb; pdb.set_trace()

        p2Ds, valids_projected = project_to_image(p3Ds, R_w2c, t_w2c, camera)
        invalid_p3D_ids = p3D_ids[p3D_ids != -1][~valids_projected]
        interp_depth, valids_backprojected = interpolate_depth(depth, p2Ds, (camera.height, camera.width))
        scs = scene_coordinates(p2Ds[valids_backprojected], R_w2c, t_w2c,
                                interp_depth[valids_backprojected],
                                camera)
        invalid_p3D_ids = np.append(
            invalid_p3D_ids,
            p3D_ids[p3D_ids != -1][valids_projected][~valids_backprojected])
        for p3did in invalid_p3D_ids:
            if p3did == -1:
                continue
            else:
                obs_imgids = points3D[p3did].image_ids
                invalid_imgids = list(np.where(obs_imgids == img.id)[0])
                points3D[p3did] = points3D[p3did]._replace(
                    image_ids=np.delete(obs_imgids, invalid_imgids),
                    point2D_idxs=np.delete(points3D[p3did].point2D_idxs,
                                           invalid_imgids))

        new_p3D_ids = p3D_ids.copy()
        sub_p3D_ids = new_p3D_ids[new_p3D_ids != -1]
        valids = np.ones(np.count_nonzero(new_p3D_ids != -1), dtype=bool)
        valids[~valids_projected] = False
        valids[valids_projected] = valids_backprojected
        sub_p3D_ids[~valids] = -1
        new_p3D_ids[new_p3D_ids != -1] = sub_p3D_ids
        img = img._replace(point3D_ids=new_p3D_ids)

        assert len(img.point3D_ids[img.point3D_ids != -1]) == len(scs), (
                f"{len(scs)}, {len(img.point3D_ids[img.point3D_ids != -1])}")
        for i, p3did in enumerate(img.point3D_ids[img.point3D_ids != -1]):
            points3D[p3did] = points3D[p3did]._replace(xyz=scs[i])
        images[imgid] = img

    output_path.mkdir(parents=True, exist_ok=True)
    write_model(cameras, images, points3D, output_path)

###############################################################################
# End of hloc utils
###############################################################################

class DepthReader(_base.BaseDepthReader):
    def __init__(self, filename, depth_folder):
        super(DepthReader, self).__init__(filename)
        self.depth_folder = depth_folder

    def read(self, filename):
        depth = PIL.Image.open(Path(self.depth_folder) / filename)
        depth = np.array(depth).astype('float64')
        depth = depth / 1000.  # mm to meter
        depth[(depth == 0.0) | (depth > 1000.0)] = np.inf
        return depth

def read_scene_ScanNet(cfg, root_path, model_path, image_path, n_neighbors=20):
    metainfos_filename = 'infos_ScanNet.npy'
    output_dir = 'tmp' if cfg['output_dir'] is None else cfg['output_dir']
    limapio.check_makedirs(output_dir)
    if cfg['skip_exists'] and os.path.exists(os.path.join(output_dir, metainfos_filename)):
        cfg['info_path'] = os.path.join(output_dir, metainfos_filename)
    if cfg['info_path'] is None:
        imagecols, neighbors, ranges = _psfm.read_infos_colmap(cfg['sfm'], root_path, model_path, image_path, n_neighbors=n_neighbors)
        with open(os.path.join(output_dir, metainfos_filename), 'wb') as f:
            np.savez(f, imagecols_np=imagecols.as_dict(), neighbors=neighbors, ranges=ranges)
    else:
        with open(cfg['info_path'], 'rb') as f:
            data = np.load(f, allow_pickle=True)
            imagecols_np, neighbors, ranges = data['imagecols_np'].item(), data['neighbors'].item(), data['ranges']
            imagecols = _base.ImageCollection(imagecols_np)
    return imagecols, neighbors, ranges
    
def get_result_filenames(cfg, use_dense_depth=False):
    ransac_cfg = cfg['ransac']
    ransac_postfix = ''
    if ransac_cfg['method'] != None:
        if ransac_cfg['method'] in ['ransac', 'hybrid']:
            ransac_postfix = '_{}'.format(ransac_cfg['method'])
        elif ransac_cfg['method'] == 'solver':
            ransac_postfix = '_sfransac'
        else:
            raise ValueError('Unsupported ransac method: {}'.format(ransac_cfg['method']))
        ransac_postfix += '_{}'.format(ransac_cfg['thres'] if ransac_cfg['method'] != 'hybrid' else '{}-{}'.format(ransac_cfg['thres_point'], ransac_cfg['thres_line']))
    results_point = 'results_{}_point.txt'.format('dense' if use_dense_depth else 'sparse')
    results_joint = 'results_{}_joint_{}{}{}{}{}.txt'.format(
            'dense' if use_dense_depth else 'sparse',
            '{}_'.format(cfg['2d_matcher']),
            '{}_'.format(cfg['reprojection_filter']) if cfg['reprojection_filter'] is not None else '',
            'filtered_' if cfg['2d_matcher'] == 'superglue_endpoints' and cfg['epipolar_filter'] else '',
            cfg['line_cost_func'],
            ransac_postfix)
    if cfg['2d_matcher'] == 'gluestick':
        results_point = results_point.replace('point', 'point_gluestick')
        results_joint = results_joint.replace('gluestick', 'gluestickp+l')
    return results_point, results_joint

def get_train_test_ids_from_sfm(full_model, blacklist=None, ext='.bin'):
    cameras, images, points3D = read_model(full_model, ext)

    if blacklist is not None:
        with open(blacklist, 'r') as f:
            blacklist = f.read().rstrip().split('\n')

    train_ids, test_ids = [], []
    for id_, image in images.items():
        if blacklist and image.name in blacklist:
            test_ids.append(id_)
        else:
            train_ids.append(id_)
    
    return train_ids, test_ids

def run_hloc_ScanNet(cfg, dataset, scene, results_file, test_list, num_covis=30, use_dense_depth=False, logger=None):
    results_dir = results_file.parent
    gt_dir = dataset / f'{scene}'

    ref_sfm_gt_pose = gt_dir / 'sfm_gt_pose'
    ref_sfm = gt_dir / 'sfm_superpoint+superglue'
    query_list = results_dir / 'query_list_with_intrinsics.txt'
    sfm_pairs = results_dir / f'pairs-db-covis{num_covis}.txt'
    depth_dir = gt_dir / 'depth'
    retrieval_path = dataset / 'ScanNet_densevlad_retrieval_top_10' / f'{scene}_top10.txt'
    feature_conf = {
        'output': 'feats-superpoint-n4096-r1024',
        'model': {'name': 'superpoint', 'nms_radius': 3, 'max_keypoints': 2048, "keypoint_threshold": 0.0},
        'preprocessing': {'globs': ['*.jpg'], 'grayscale': True, 'resize_max': 1024}
    }
    if cfg['localization']['2d_matcher'] == 'gluestick':
        raise ValueError("GlueStick not yet supported in HLoc.")
        # matcher_conf = match_features.confs['gluestick']
    else:
        matcher_conf = match_features.confs['superglue']
        matcher_conf['model']['sinkhorn_iterations'] = 5
    
    # create_reference_sfm(gt_dir, ref_sfm_gt_pose, test_list)
    create_reference_sfm_from_ScanNetDatset(gt_dir, ref_sfm_gt_pose)
    train_ids, query_ids = get_train_test_ids_from_sfm(ref_sfm_gt_pose, test_list)
    # create_query_list_with_intrinsics(gt_dir, query_list, test_list)
    
    # feature extraction
    features = extract_features.main(
            feature_conf, dataset / f'{scene}/color', results_dir, as_half=True)
    
    if not sfm_pairs.exists():
        # pairs_from_covisibility.main(
        #         ref_sfm_gt_pose, sfm_pairs, num_matched=num_covis)
        pairs_from_poses.main(ref_sfm_gt_pose, sfm_pairs, num_matched=num_covis)
    sfm_matches = match_features.main(
            matcher_conf, sfm_pairs, feature_conf['output'], results_dir)
    # loc_matches = match_features.main(
    #         matcher_conf, retrieval_path, feature_conf['output'], results_dir)
    if not ref_sfm.exists():
        triangulation.main(
                ref_sfm, ref_sfm_gt_pose, dataset / scene, sfm_pairs, features, sfm_matches)

    if use_dense_depth:
        assert depth_dir is not None
        ref_sfm_fix = gt_dir / 'sfm_superpoint+superglue+depth'
        if not cfg['skip_exists'] or not ref_sfm_fix.exists():
            correct_sfm_with_gt_depth(ref_sfm, depth_dir, ref_sfm_fix)
        ref_sfm = ref_sfm_fix

    ref_sfm = pycolmap.Reconstruction(ref_sfm)

    '''
    if not (cfg['skip_exists'] or cfg['localization']['hloc']['skip_exists']) or not os.path.exists(results_file):
        # point only localization
        if logger: logger.info('Running Point-only localization...')
        localize_sfm.main(
            ref_sfm, query_list, retrieval_path, features, loc_matches, results_file, covisibility_clustering=False, prepend_camera_name=True)
        if logger: logger.info(f'Coarse pose saved at {results_file}')
        evaluate(gt_dir, results_file, test_list)
    else:
        if logger: logger.info(f'Point-only localization skipped.')
    '''
    # Read coarse poses
    poses = {}
    '''
    with open(results_file, 'r') as f:
        lines = []
        for data in f.read().rstrip().split('\n'):
            data = data.split()
            name = data[0]
            q, t = np.split(np.array(data[1:], float), [4])
            poses[name] = _base.CameraPose(q, t)
    if logger: logger.info(f'Coarse pose read from {results_file}')
    '''
    hloc_log_file = f'{results_file}_logs.pkl'

    return poses, hloc_log_file, {'train': train_ids, 'query': query_ids}