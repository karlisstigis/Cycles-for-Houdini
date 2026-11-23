// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/session.h"
#include "cycles_houdini/image_iris.h"
#include "dispersion_contracts.h"
#include "gpu_concurrency_contracts.h"
#include "volume_fixture.h"
#include "point_curve_fixture.h"
#include "gaussian_splat_contracts.h"
#include "gaussian_splat_validation_contracts.h"
#include "motion_fixture.h"
#include "volume_motion_fixture.h"
#include "point_lifetime_fixture.h"
#include "strand_material_fixture.h"
#include "volume_material_fixture.h"
#include "volume_vector_contracts.h"
#include "volume_wire_contracts.h"
#include "displacement_fixture.h"
#include "displacement_motion_fixture.h"
#include "subdivision_fixture.h"
#include "grid_benchmark.h"
#include "cycles_houdini/shader.h"
#include "shader_fixture.h"
#include "named_attribute_fixture.h"
#include "tangent_reference.h"
#include "spatial_reference.h"
#include "camera_lens_reference.h"
#include "camera_dof_motion_contracts.h"
#include "camera_intrinsic_motion_contracts.h"
#include "camera_sampling_contracts.h"
#include "panorama_contracts.h"
#include "snapshot_contracts.h"
#include "object_binding_contracts.h"
#include "object_pose_contracts.h"
#include "object_attribute_contracts.h"
#include "attribute_eligibility_contracts.h"
#include "object_attribute_dlss_contracts.h"
#include "object_volume_bounds_contracts.h"
#include "mesh_payload_contracts.h"
#include "mesh_sharing_contracts.h"
#include "mesh_family_scale_contracts.h"
#include "compact_image_contracts.h"
#include "owned_image_contracts.h"
#include "displacement_hash_contracts.h"
#include "hue_ramp_contracts.h"
#include "ies_ownership_contracts.h"
#include "pointiness_contracts.h"
#include "subdivision_shading_contracts.h"
#include "light_tree_cache_contracts.h"
#include "device_range_contracts.h"
#include "instance_motion_contracts.h"
#include "session_lifecycle_contracts.h"
#include "compositing_fixture.h"
#include "noise_reference.h"
#include "showcase_volume.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <thread>

using namespace cycles_houdini;
namespace cycles_houdini::harness {
void image_iris_render_contracts(const std::string &device, const std::string &output);
void image_iris_color_render_contracts(const std::string &device, const std::string &output);
} // namespace cycles_houdini::harness
namespace {
std::string json_string(const std::string &s) {
  std::string out = "\"";
  for (char c : s) {
    if (c == '"' || c == '\\') {
      out += '\\';
      out += c;
    } else if (c == '\n')
      out += "\\n";
    else if (c == '\r')
      out += "\\r";
    else if (c == '\t')
      out += "\\t";
    else
      out += c;
  }
  return out + '"';
}
void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
void wait_complete(RenderSession &session, std::uint64_t generation, int timeout_seconds) {
  const auto start = std::chrono::steady_clock::now();
  while (true) {
    auto status = session.status();
    if (status.state == SessionState::Failed)
      throw std::runtime_error(status.error);
    auto frame = session.frame();
    if (frame && frame->generation == generation && frame->complete)
      return;
    if (std::chrono::steady_clock::now() - start > std::chrono::seconds(timeout_seconds)) {
      session.cancel();
      throw std::runtime_error("Render timeout: " + status.message);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
}
void check_fixture(const FrameEvent &frame) {
  require(!frame.planes.empty(), "Missing image");
  const auto &beauty = frame.planes.front();
  double energy = 0;
  for (float f : beauty.pixels) {
    require(std::isfinite(f), "Non-finite image");
    energy += std::abs(f);
  }
  require(energy > 0, "Blank image");
  const auto pixel = (static_cast<std::size_t>(frame.height / 2) * frame.width + frame.width / 2) * 4;
  require(beauty.pixels[pixel] > beauty.pixels[pixel + 1] * 2, "Independent expectation: centre triangle must be red");
  require(beauty.pixels[pixel + 3] > 0.99f, "Independent expectation: centre alpha must be opaque");
}
void report(const SessionStatus &s, const std::string &device) {
  std::cout << "{\"state\":" << json_string(state_name(s.state)) << ",\"device\":" << json_string(device)
            << ",\"generation\":" << s.generation << ",\"first_image_seconds\":" << s.first_image_seconds
            << ",\"elapsed_seconds\":" << s.elapsed_seconds << ",\"last_update_seconds\":" << s.last_update_seconds
            << ",\"geometry_conversions\":" << s.geometry_conversions << ",\"object_updates\":" << s.object_updates
            << ",\"owned_geometry_bytes\":" << s.owned_geometry_bytes
            << ",\"device_allocation_bytes\":" << s.device_allocation_bytes
            << ",\"device_allocation_peak_bytes\":" << s.device_allocation_peak_bytes << "}\n";
}
} // namespace
int main(int argc, char **argv) {
  try {
    std::string resources, device, output = "calibration.exr", volume_fixture, pyro_fixture, showcase_volume,
                                   volume_motion_fixtures, fixture = "calibration", gpu_concurrency_mode;
    bool list = false, lifecycle = false, motion_test = false, volume_motion_test = false, point_lifetime = false,
         strand_materials = false, volume_materials = false, displacement = false, subdivision = false,
         grid_test = false, diagnostics = false, process_memory = false, catalogue = false, compositing = false,
         noise_reference = false;
    bool session_lifecycle = false;
    bool persistent_updates = false, displacement_motion = false, named_attributes = false, tangent_reference = false,
         snapshot_contracts = false, object_bindings = false;
    bool displacement_subdivision_normals_only = false;
    bool object_volume_bounds = false, attribute_eligibility = false;
    bool compact_images = false, object_poses = false, object_attributes = false, object_attribute_dlss = false,
         owned_images = false, displacement_hash = false, volume_vectors = false, volume_wire = false;
    bool ies_ownership = false, spatial_reference = false, camera_lens_reference = false,
         camera_sampling_contracts = false, panorama_contracts = false, pointiness_contracts = false;
    bool subdivision_shading = false, hue_ramps = false, mesh_payloads = false, mesh_sharing = false,
         mesh_family_scale = false, light_tree_blocks = false, device_ranges = false, instance_motion = false;
    std::vector<std::string> instance_motion_devices;
    bool camera_dof_motion = false;
    std::vector<std::string> camera_dof_devices;
    bool iris_contracts = false, iris_render = false, iris_color_render = false, dispersion_contract = false,
         gaussian_splats = false;
    bool camera_intrinsic_motion = false;
    std::vector<std::string> camera_intrinsic_devices;
    int grid_rows = 501, grid_columns = 1001, repetitions = 30;
    int width = 320, height = 180, samples = 16, timeout = 180;
    DenoiseSettings denoising;
    for (int a = 1; a < argc; ++a) {
      const std::string arg = argv[a];
      auto value = [&]() -> std::string {
        if (a + 1 >= argc)
          throw std::invalid_argument("Missing value for " + arg);
        return argv[++a];
      };
      if (arg == "--resources")
        resources = value();
      else if (arg == "--device")
        device = value();
      else if (arg == "--output")
        output = value();
      else if (arg == "--width")
        width = std::stoi(value());
      else if (arg == "--height")
        height = std::stoi(value());
      else if (arg == "--samples")
        samples = std::stoi(value());
      else if (arg == "--timeout")
        timeout = std::stoi(value());
      else if (arg == "--list-devices")
        list = true;
      else if (arg == "--lifecycle-test")
        lifecycle = true;
      else if (arg == "--write-volume-fixture")
        volume_fixture = value();
      else if (arg == "--write-pyro-fixture")
        pyro_fixture = value();
      else if (arg == "--write-showcase-volume")
        showcase_volume = value();
      else if (arg == "--write-volume-motion-fixtures")
        volume_motion_fixtures = value();
      else if (arg == "--fixture")
        fixture = value();
      else if (arg == "--motion-test")
        motion_test = true;
      else if (arg == "--volume-motion-test")
        volume_motion_test = true;
      else if (arg == "--dispersion-contracts")
        dispersion_contract = true;
      else if (arg == "--gpu-concurrency-contracts")
        gpu_concurrency_mode = value();
      else if (arg == "--gaussian-splat-contracts")
        gaussian_splats = true;
      else if (arg == "--point-lifetime-test")
        point_lifetime = true;
      else if (arg == "--strand-material-test")
        strand_materials = true;
      else if (arg == "--volume-material-test")
        volume_materials = true;
      else if (arg == "--volume-vector-contracts")
        volume_vectors = true;
      else if (arg == "--volume-wire-contracts")
        volume_wire = true;
      else if (arg == "--displacement-test")
        displacement = true;
      else if (arg == "--displacement-motion-reference")
        displacement_motion = true;
      else if (arg == "--subdivision-normals-only") {
        displacement_motion = true;
        displacement_subdivision_normals_only = true;
      } else if (arg == "--named-attribute-contracts")
        named_attributes = true;
      else if (arg == "--tangent-reference")
        tangent_reference = true;
      else if (arg == "--spatial-displacement-reference")
        spatial_reference = true;
      else if (arg == "--camera-lens-reference")
        camera_lens_reference = true;
      else if (arg == "--camera-dof-motion-contracts")
        camera_dof_motion = true;
      else if (arg == "--image-iris-contracts")
        iris_contracts = true;
      else if (arg == "--image-iris-render")
        iris_render = true;
      else if (arg == "--image-iris-color-render")
        iris_color_render = true;
      else if (arg == "--camera-dof-device")
        camera_dof_devices.push_back(value());
      else if (arg == "--camera-intrinsic-motion-contracts")
        camera_intrinsic_motion = true;
      else if (arg == "--camera-intrinsic-device")
        camera_intrinsic_devices.push_back(value());
      else if (arg == "--camera-sampling-contracts")
        camera_sampling_contracts = true;
      else if (arg == "--panorama-contracts")
        panorama_contracts = true;
      else if (arg == "--snapshot-contracts")
        snapshot_contracts = true;
      else if (arg == "--object-binding-contracts")
        object_bindings = true;
      else if (arg == "--object-pose-contracts")
        object_poses = true;
      else if (arg == "--object-attribute-contracts")
        object_attributes = true;
      else if (arg == "--attribute-eligibility-contracts")
        attribute_eligibility = true;
      else if (arg == "--object-attribute-dlss-contracts")
        object_attribute_dlss = true;
      else if (arg == "--object-volume-bounds-contracts")
        object_volume_bounds = true;
      else if (arg == "--mesh-payload-contracts")
        mesh_payloads = true;
      else if (arg == "--mesh-sharing-contracts")
        mesh_sharing = true;
      else if (arg == "--mesh-family-scale")
        mesh_family_scale = true;
      else if (arg == "--compact-image-contracts")
        compact_images = true;
      else if (arg == "--owned-image-contracts")
        owned_images = true;
      else if (arg == "--displacement-hash-contracts")
        displacement_hash = true;
      else if (arg == "--hue-ramp-contracts")
        hue_ramps = true;
      else if (arg == "--ies-ownership-contracts")
        ies_ownership = true;
      else if (arg == "--mesh-pointiness-contracts")
        pointiness_contracts = true;
      else if (arg == "--subdivision-shading-contracts")
        subdivision_shading = true;
      else if (arg == "--light-tree-cache-contracts")
        light_tree_blocks = true;
      else if (arg == "--device-range-contracts")
        device_ranges = true;
      else if (arg == "--instance-motion-contracts")
        instance_motion = true;
      else if (arg == "--instance-motion-device")
        instance_motion_devices.push_back(value());
      else if (arg == "--subdivision-test")
        subdivision = true;
      else if (arg == "--grid-benchmark")
        grid_test = true;
      else if (arg == "--diagnostics")
        diagnostics = true;
      else if (arg == "--persistent-updates")
        persistent_updates = true;
      else if (arg == "--session-lifecycle-contracts")
        session_lifecycle = true;
      else if (arg == "--process-memory")
        process_memory = true;
      else if (arg == "--shader-catalogue")
        catalogue = true;
      else if (arg == "--compositing-test")
        compositing = true;
      else if (arg == "--noise-reference")
        noise_reference = true;
      else if (arg == "--denoiser") {
        denoising.method = value();
        denoising.enabled = true;
      } else if (arg == "--denoise-cpu")
        denoising.use_gpu = false;
      else if (arg == "--denoise-quality")
        denoising.quality = value();
      else if (arg == "--denoise-prefilter")
        denoising.prefilter = value();
      else if (arg == "--denoise-start")
        denoising.start_sample = std::stoi(value());
      else if (arg == "--rows")
        grid_rows = std::stoi(value());
      else if (arg == "--cols")
        grid_columns = std::stoi(value());
      else if (arg == "--repetitions")
        repetitions = std::stoi(value());
      else if (arg == "--help") {
        std::cout << "ch_harness --resources PATH --list-devices | --device ID --output FILE.exr [--lifecycle-test] "
                     "[--samples N] [--width W --height H]\n";
        return 0;
      } else
        throw std::invalid_argument("Unknown argument: " + arg);
    }
    if (!volume_fixture.empty()) {
      harness::write_density_fixture(volume_fixture);
      return 0;
    }
    if (!pyro_fixture.empty()) {
      harness::write_pyro_fixture(pyro_fixture);
      return 0;
    }
    if (!showcase_volume.empty()) {
      harness::write_showcase_volume(showcase_volume);
      return 0;
    }
    if (!volume_motion_fixtures.empty()) {
      harness::write_volume_motion_fixtures(volume_motion_fixtures);
      return 0;
    }
    if (noise_reference) {
      harness::write_noise_reference(output);
      return 0;
    }
    if (hue_ramps) {
      harness::hue_ramp_contracts();
      return 0;
    }
    if (iris_contracts) {
      image_iris_contracts(output);
      return 0;
    }
    if (dispersion_contract) {
      harness::dispersion_contracts();
      return 0;
    }
    if (!gpu_concurrency_mode.empty()) {
      harness::gpu_concurrency_contracts(gpu_concurrency_mode);
      return 0;
    }
    if (resources.empty())
      resources = std::filesystem::absolute(argv[0]).parent_path().string();
    enable_renderer_device_probes();
    initialize(resources);
    if (catalogue) {
      std::ofstream file(output);
      if (!file)
        throw std::runtime_error("Cannot open shader catalogue output");
      file << shader_catalogue_json() << '\n';
      if (!file)
        throw std::runtime_error("Cannot write shader catalogue");
      return 0;
    }
    const auto caps = capabilities();
    if (list) {
      std::cout << "[";
      bool first = true;
      for (const auto &d : caps.devices) {
        if (!first)
          std::cout << ",";
        first = false;
        std::cout << "{\"id\":" << json_string(d.id) << ",\"name\":" << json_string(d.name)
                  << ",\"backend\":" << json_string(d.backend)
                  << ",\"display_device\":" << (d.display_device ? "true" : "false") << "}";
      }
      std::cout << "]\n";
      return 0;
    }
    if (device.empty())
      throw std::invalid_argument("An explicit --device ID is required. Use --list-devices.");
    if (iris_render) {
      harness::image_iris_render_contracts(device, output);
      return 0;
    }
    if (gaussian_splats) {
      harness::gaussian_splat_validation_contracts(device);
      harness::gaussian_splat_contracts(device, output);
      return 0;
    }
    if (iris_color_render) {
      harness::image_iris_color_render_contracts(device, output);
      return 0;
    }
    if (motion_test) {
      harness::motion_reference_test(device, output);
      return 0;
    }
    if (named_attributes) {
      harness::named_attribute_contract_test(device);
      return 0;
    }
    if (session_lifecycle) {
      harness::session_lifecycle_contracts(device);
      return 0;
    }
    if (snapshot_contracts) {
      harness::snapshot_contracts();
      return 0;
    }
    if (volume_vectors) {
      harness::volume_vector_contracts(device);
      return 0;
    }
    if (volume_wire) {
      harness::volume_wire_contracts();
      return 0;
    }
    if (object_bindings) {
      harness::object_binding_contracts(device);
      return 0;
    }
    if (object_poses) {
      harness::object_pose_contracts(device);
      return 0;
    }
    if (object_attributes) {
      harness::object_attribute_contracts(device);
      return 0;
    }
    if (attribute_eligibility) {
      harness::attribute_eligibility_contracts(device);
      return 0;
    }
    if (object_attribute_dlss) {
      harness::object_attribute_dlss_contracts(device);
      return 0;
    }
    if (object_volume_bounds) {
      harness::object_volume_bounds_contracts(device);
      return 0;
    }
    if (mesh_payloads) {
      harness::mesh_payload_contracts();
      return 0;
    }
    if (mesh_sharing) {
      harness::mesh_sharing_contracts(device);
      return 0;
    }
    if (mesh_family_scale) {
      harness::mesh_family_scale_contracts(device);
      return 0;
    }
    if (compact_images) {
      harness::compact_image_contracts();
      return 0;
    }
    if (owned_images) {
      harness::owned_image_contracts(device);
      return 0;
    }
    if (displacement_hash) {
      harness::displacement_hash_contracts();
      return 0;
    }
    if (ies_ownership) {
      harness::ies_ownership_contracts();
      return 0;
    }
    if (pointiness_contracts) {
      harness::mesh_pointiness_contracts();
      return 0;
    }
    if (subdivision_shading) {
      harness::subdivision_shading_contracts();
      return 0;
    }
    if (light_tree_blocks) {
      harness::light_tree_cache_contracts();
      return 0;
    }
    if (device_ranges) {
      harness::device_range_contracts(device);
      return 0;
    }
    if (instance_motion) {
      instance_motion_devices.insert(instance_motion_devices.begin(), device);
      harness::instance_motion_contracts(instance_motion_devices);
      return 0;
    }
    if (tangent_reference) {
      harness::tangent_reference_images(device, output);
      return 0;
    }
    if (spatial_reference) {
      harness::spatial_displacement_reference(device, output);
      return 0;
    }
    if (camera_lens_reference) {
      harness::camera_lens_reference(device, output);
      return 0;
    }
    if (camera_dof_motion) {
      camera_dof_devices.insert(camera_dof_devices.begin(), device);
      harness::camera_dof_motion_contracts(camera_dof_devices, output);
      return 0;
    }
    if (camera_intrinsic_motion) {
      camera_intrinsic_devices.insert(camera_intrinsic_devices.begin(), device);
      harness::camera_intrinsic_motion_contracts(camera_intrinsic_devices, output);
      return 0;
    }
    if (camera_sampling_contracts) {
      harness::camera_sampling_contracts(device);
      return 0;
    }
    if (panorama_contracts) {
      harness::panorama_contracts(device);
      return 0;
    }
    if (volume_motion_test) {
      harness::volume_motion_reference_test(device, output);
      return 0;
    }
    if (point_lifetime) {
      harness::point_lifetime_test(device, output);
      return 0;
    }
    if (strand_materials) {
      harness::strand_material_test(device, output);
      return 0;
    }
    if (volume_materials) {
      harness::volume_material_test(device, output);
      return 0;
    }
    if (displacement) {
      harness::displacement_test(device, output);
      return 0;
    }
    if (displacement_motion) {
      harness::displacement_motion_reference(device, output, displacement_subdivision_normals_only);
      return 0;
    }
    if (subdivision) {
      harness::subdivision_test(device, output);
      return 0;
    }
    if (compositing) {
      harness::compositing_reference_test(device, output);
      return 0;
    }
    if (grid_test) {
      harness::grid_benchmark(device, output, grid_rows, grid_columns, repetitions, samples, diagnostics,
                              process_memory);
      return 0;
    }
    if (fixture != "calibration" && fixture != "points" && fixture != "hair" && fixture != "shader-reference")
      throw std::invalid_argument("Unknown reference fixture: " + fixture);
    if (lifecycle && fixture != "calibration")
      throw std::invalid_argument("Lifecycle mode currently uses the calibration fixture");
    SessionConfig config;
    config.device_id = device;
    config.width = width;
    config.height = height;
    config.samples = samples;
    config.interactive = lifecycle;
    config.persistent_updates = persistent_updates;
    config.collect_diagnostics = diagnostics;
    config.passes = {"combined", "depth", "normal", "uv", "position"};
    config.denoising = denoising;
    RenderSession session(config);
    auto initial = fixture == "calibration" ? calibration_fixture()
                   : fixture == "shader-reference"
                       ? harness::shader_reference_fixture()
                       : harness::point_curve_fixture(fixture == "hair", float(width) / height);
    auto generation = session.apply(initial);
    wait_complete(session, generation, timeout);
    if (fixture == "calibration")
      check_fixture(*session.frame());
    session.write_exr(output);
    report(session.status(), device);
    if (lifecycle) {
      const auto conversions = session.status().geometry_conversions;
      for (int iteration = 0; iteration < 8; ++iteration) {
        SceneUpdate edit;
        edit.camera = CameraData{};
        edit.camera->transform[3] = (iteration % 2 ? 0.1f : -0.1f);
        generation = session.apply(edit);
        wait_complete(session, generation, timeout);
        require(session.status().geometry_conversions == conversions, "Camera edit reconverted geometry");
        require(session.frame()->generation == generation, "Stale frame generation");
        report(session.status(), device);
      }
      SceneUpdate invalid;
      invalid.deleted_geometry = {"calibration/triangle"};
      if (persistent_updates)
        require(session.diagnostics().session_cancel_calls == 0,
                "Ordinary persistent updates cancelled the session loop");
      bool rejected = false;
      try {
        session.apply(invalid);
      } catch (const std::invalid_argument &) {
        rejected = true;
      }
      require(rejected, "Deleting bound geometry was accepted");
      require(session.status().generation == generation, "Invalid update advanced generation");
      session.pause(true);
      require(session.status().state == SessionState::Paused, "Pause state lost");
      session.pause(false);
      SceneUpdate edit;
      edit.camera = CameraData{};
      generation = session.apply(edit);
      session.cancel();
      require(session.status().state == SessionState::Cancelled, "Cancellation state lost");
      generation = session.apply(edit);
      wait_complete(session, generation, timeout);
      require(session.frame()->generation == generation, "Restart delivered stale image");
      SceneUpdate remove;
      remove.deleted_objects = {"calibration/triangle"};
      remove.deleted_geometry = {"calibration/triangle"};
      remove.deleted_materials = {"calibration/red"};
      generation = session.apply(remove);
      wait_complete(session, generation, timeout);
      require(session.status().owned_geometry_bytes == 0, "Geometry retirement left owned buffers");
      generation = session.apply(initial);
      wait_complete(session, generation, timeout);
      check_fixture(*session.frame());
      session.write_exr(output);
      report(session.status(), device);
    }
    session.close();
    require(session.status().state == SessionState::Closed, "Close state lost");
    session.close();
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "Cycles Houdini: " << e.what() << '\n';
    return 1;
  }
}
