#include "base/image_collection.h"

namespace limap {

void ImageCollection::init_cameras_from_vector(const std::vector<Camera>& input_cameras) {
    cameras.clear();
    size_t n_cameras = input_cameras.size();
    for (size_t cam_id = 0; cam_id < n_cameras; ++cam_id) {
        cameras.insert(std::make_pair(cam_id, input_cameras[cam_id]));
    }
}

void ImageCollection::init_images_from_vector(const std::vector<CameraImage>& input_images) {
    images.clear();
    size_t n_cameras = input_images.size();
    for (size_t img_id = 0; img_id < n_cameras; ++img_id) {
        images.insert(std::make_pair(img_id, input_images[img_id]));
    }
}

ImageCollection::ImageCollection(const std::map<int, Camera>& input_cameras, const std::vector<CameraImage>& input_images) {
    cameras = input_cameras;
    init_images_from_vector(input_images);
}

ImageCollection::ImageCollection(const std::vector<Camera>& input_cameras, const std::map<int, CameraImage>& input_images) {
    init_cameras_from_vector(input_cameras);
    images = input_images;
}

ImageCollection::ImageCollection(const std::vector<Camera>& input_cameras, const std::vector<CameraImage>& input_images) {
    init_cameras_from_vector(input_cameras);
    init_images_from_vector(input_images);
}

ImageCollection::ImageCollection(const std::vector<CameraView>& camviews) {
    size_t n_images = camviews.size();
    for (size_t img_id = 0; img_id < n_images; ++img_id) {
        auto camview = camviews[img_id];
        images.insert(std::make_pair(img_id, CameraImage(camview)));
        int cam_id = camview.cam.CameraId();
        if (exist_cam(cam_id)) {
            CHECK_EQ(cameras.at(cam_id) == camview.cam, true);
        }
        else {
            cameras.insert(std::make_pair(camview.cam.CameraId(), camview.cam));
        }
    }
}

ImageCollection::ImageCollection(py::dict dict) {
    // load cameras
    std::map<int, py::dict> dictvec_cameras;
    if (dict.contains("cameras"))
        dictvec_cameras = dict["cameras"].cast<std::map<int, py::dict>>();
    for (auto it = dictvec_cameras.begin(); it != dictvec_cameras.end(); ++it) {
        int cam_id = it->first;
        Camera cam = Camera(it->second);
        assert (cam_id == cam.CameraId());
        cameras.insert(std::make_pair(cam_id, cam));
    }
    // load images
    std::map<int, py::dict> dictvec_images;
    if (dict.contains("images"))
        dictvec_images = dict["images"].cast<std::map<int, py::dict>>();
    for (auto it = dictvec_images.begin(); it != dictvec_images.end(); ++it) {
        int img_id = it->first;
        CameraImage camimage = CameraImage(it->second);
        images.insert(std::make_pair(img_id, camimage));
    }
}

std::vector<Camera> ImageCollection::get_cameras() const {
    std::vector<Camera> output_cameras;
    for (auto it = cameras.begin(); it != cameras.end(); ++it) {
        output_cameras.push_back(it->second);
    }
    return output_cameras;
}

std::vector<int> ImageCollection::get_cam_ids() const {
    std::vector<int> output_ids;
    for (auto it = cameras.begin(); it != cameras.end(); ++it) {
        output_ids.push_back(it->first);
    }
    return output_ids;
}

std::vector<CameraImage> ImageCollection::get_images() const {
    std::vector<CameraImage> output_images;
    for (auto it = images.begin(); it != images.end(); ++it) {
        output_images.push_back(it->second);
    }
    return output_images;
}

std::vector<int> ImageCollection::get_img_ids() const {
    std::vector<int> output_ids;
    for (auto it = images.begin(); it != images.end(); ++it) {
        output_ids.push_back(it->first);
    }
    return output_ids;
}

ImageCollection::ImageCollection(const ImageCollection& imagecols) {
    std::vector<int> cam_ids = imagecols.get_cam_ids();
    for (auto it = cam_ids.begin(); it != cam_ids.end(); ++it) {
        cameras.insert(std::make_pair(*it, imagecols.cam(*it)));
    }
    std::vector<int> img_ids = imagecols.get_img_ids();
    for (auto it = img_ids.begin(); it != img_ids.end(); ++it) {
        images.insert(std::make_pair(*it, imagecols.camimage(*it)));
    }
}

std::vector<CameraView> ImageCollection::get_camviews() const {
    std::vector<CameraView> camviews;
    for (auto it = images.begin(); it != images.end(); ++it) {
        camviews.push_back(camview(it->first));
    }
    return camviews;
}

py::dict ImageCollection::as_dict() const {
    py::dict output;
    std::map<int, py::dict> dictvec_cameras;
    for (auto it = cameras.begin(); it != cameras.end(); ++it) {
        dictvec_cameras.insert(std::make_pair(it->first, it->second.as_dict()));
    }
    output["cameras"] = dictvec_cameras;
    std::map<int, py::dict> dictvec_images;
    for (auto it = images.begin(); it != images.end(); ++it) {
        dictvec_images.insert(std::make_pair(it->first, it->second.as_dict()));
    }
    output["images"] = dictvec_images;
    return output;
}

Camera ImageCollection::cam(const int cam_id) const {
    THROW_CHECK_EQ(exist_cam(cam_id), true);
    return cameras.at(cam_id);
}

bool ImageCollection::exist_cam(const int cam_id) const {
    return cameras.count(cam_id) == 1;
}

CameraImage ImageCollection::camimage(const int img_id) const {
    THROW_CHECK_EQ(exist_image(img_id), true);
    return images.at(img_id);
}

bool ImageCollection::exist_image(const int img_id) const {
    return images.count(img_id) == 1;
}

CameraPose ImageCollection::campose(const int img_id) const {
    THROW_CHECK_EQ(exist_image(img_id), true);
    return images.at(img_id).pose;
}

CameraView ImageCollection::camview(const int img_id) const {
    THROW_CHECK_EQ(exist_image(img_id), true);
    int cam_id = images.at(img_id).cam_id;
    return CameraView(cameras.at(cam_id), images.at(img_id).pose, images.at(img_id).image_name());
}

std::string ImageCollection::image_name(const int img_id) const {
    THROW_CHECK_EQ(exist_image(img_id), true);
    return images.at(img_id).image_name();
}

std::vector<std::string> ImageCollection::get_image_list() const {
    std::vector<std::string> image_names;
    for (auto it = images.begin(); it != images.end(); ++it) {
        image_names.push_back(image_name(it->first));
    }
    return image_names;
}

py::array_t<uint8_t> ImageCollection::read_image(const int img_id, const bool set_gray) const {
    return camview(img_id).read_image(set_gray);
}

void ImageCollection::set_max_image_dim(const int& val) {
    for (auto it = cameras.begin(); it != cameras.end(); ++it) {
        it->second.set_max_image_dim(val);
    }
}

void ImageCollection::change_camera(const int cam_id, const Camera cam) {
    THROW_CHECK_EQ(exist_cam(cam_id), true);
    cameras[cam_id] = cam;
}

void ImageCollection::change_image(const int img_id, const CameraImage camimage) {
    THROW_CHECK_EQ(exist_image(img_id), true);
    images[img_id] = camimage;
}

void ImageCollection::change_image_name(const int img_id, const std::string new_name) {
    THROW_CHECK_EQ(exist_image(img_id), true);
    images[img_id].SetImageName(new_name);
}

bool ImageCollection::IsUndistorted() const {
    for (auto it = cameras.begin(); it != cameras.end(); ++it) {
        if (!it->second.IsUndistorted())
            return false;
    }
    return true;
}

} // namespace limap

