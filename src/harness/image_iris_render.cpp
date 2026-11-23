// SPDX-License-Identifier: Apache-2.0
#include "motion_fixture.h"
#include "cycles_houdini/image_iris.h"
#include "device/device.h"
#include "scene/camera.h"
#include "scene/integrator.h"
#include "scene/scene.h"
#include "scene/tables.h"
#include "util/profiling.h"
#include "util/progress.h"
#include <OpenImageIO/imageio.h>
#include <array>
#include <fstream>
#include <iomanip>
#include <numbers>

namespace cycles_houdini::harness {
namespace {
using Clock = std::chrono::steady_clock;
void check_progressive_camera_lookup_tables() {
  auto devices = ccl::Device::available_devices(ccl::DEVICE_MASK_CPU);
  motion_require(!devices.empty(), "CPU device unavailable for the camera resize fixture");
  ccl::Stats stats;
  ccl::Profiler profiler;
  auto device = ccl::Device::create(devices.front(), stats, profiler, true);
  motion_require(device && !device->have_error() && device->load_kernels(0), "Camera resize fixture device failed");
  ccl::Scene scene(ccl::SceneParams{}, device.get());
  scene.camera->set_full_width(1280);
  scene.camera->set_full_height(720);
  scene.integrator->set_motion_blur(true);
  // Device update stores opaque table data. The render fixture below exercises sampling.
  ccl::array<float> iris;
  iris.resize(1);
  iris[0] = 1;
  scene.camera->set_image_iris(iris);
  scene.camera->set_image_iris_method(1);
  scene.camera->device_update(device.get(), &scene.dscene, &scene);
  scene.lookup_tables->device_update(device.get(), &scene.dscene, &scene);
  const auto initial = scene.dscene.data.cam;
  motion_require(initial.image_iris_offset >= 0 && initial.shutter_table_offset >= 0,
                 "Camera resize fixture did not install both lookup tables");
  ccl::Progress progress;
  for (const auto &[width, height] : {std::pair{640, 360}, std::pair{1280, 720}}) {
    motion_require(scene.update_camera_resolution(progress, width, height), "Progressive screen size did not update");
    const auto &camera = scene.dscene.data.cam;
    motion_require(camera.width == width && camera.height == height, "Progressive camera retained its prior raster");
    motion_require(camera.image_iris_offset == initial.image_iris_offset &&
                       camera.shutter_table_offset == initial.shutter_table_offset &&
                       !scene.lookup_tables->need_update(),
                   "Progressive camera resizing invalidated an unchanged lookup table");
  }
}

struct Moments {
  double energy = 0, x = 0, y = 0, xx = 0, yy = 0, xy = 0;
};
Moments moments(const FrameEvent &frame) {
  Moments m;
  const auto values = expand_image_plane(frame, frame.planes.front());
  for (int y = 0; y < frame.height; ++y)
    for (int x = 0; x < frame.width; ++x) {
      const double value = values[(std::size_t(y) * frame.width + x) * 4];
      motion_require(std::isfinite(value) && value >= 0, "Image iris produced invalid radiance");
      m.energy += value;
      m.x += x * value;
      m.y += y * value;
      m.xx += x * x * value;
      m.yy += y * y * value;
      m.xy += x * y * value;
    }
  motion_require(m.energy > 0, "Image iris produced an empty defocus image");
  m.x /= m.energy;
  m.y /= m.energy;
  m.xx = m.xx / m.energy - m.x * m.x;
  m.yy = m.yy / m.energy - m.y * m.y;
  m.xy = m.xy / m.energy - m.x * m.y;
  return m;
}
double image_difference(const FrameEvent &a, const FrameEvent &b) {
  const auto x = expand_image_plane(a, a.planes.front()), y = expand_image_plane(b, b.planes.front());
  double sum = 0;
  for (std::size_t i = 0; i < x.size(); i += 4)
    sum += std::abs(x[i] - y[i]);
  return sum / (a.width * a.height);
}
std::array<double, 2> aperture_centroid(const std::filesystem::path &filename) {
  auto input = OIIO::ImageInput::open(filename.string());
  motion_require(bool(input), "Cannot read orientation reference");
  const auto spec = input->spec();
  std::vector<float> image(std::size_t(spec.width) * spec.height);
  motion_require(input->read_image(0, 0, 0, 1, OIIO::TypeDesc::FLOAT, image.data()),
                 "Cannot decode orientation reference");
  input->close();
  double mass = 0, mx = 0, my = 0;
  for (int y = 0; y < spec.height - 1; ++y)
    for (int x = 0; x < spec.width - 1; ++x) {
      const double a = image[y * spec.width + x], b = image[y * spec.width + x + 1],
                   c = image[(y + 1) * spec.width + x], d = image[(y + 1) * spec.width + x + 1];
      const double m = (a + b + c + d) / 4;
      mass += m;
      mx += 2 * (x * m + (a + c + 2 * b + 2 * d) / 12) / (spec.width - 1) - m;
      my += m - 2 * (y * m + (a + b + 2 * c + 2 * d) / 12) / (spec.height - 1);
    }
  return {mx / mass, my / mass};
}
void write_mask(const std::filesystem::path &filename, int shape, float scale = 1) {
  constexpr int width = 33, height = 33;
  std::vector<float> pixels(width * height);
  for (int y = 0; y < height; ++y)
    for (int x = 0; x < width; ++x) {
      const float u = 2.f * x / (width - 1) - 1, v = 2.f * y / (height - 1) - 1;
      float value = 1;
      if (shape == 1)
        value = (std::abs(u + .45f) < .28f && std::abs(v - .30f) < .48f) ||
                        (std::abs(u - .45f) < .22f && std::abs(v + .4f) < .18f)
                    ? 1.f
                    : 0.f;
      if (shape == 2)
        value = std::abs(u) < .8f && std::abs(v) < .15f ? 1.f : 0.f;
      if (shape == 3)
        value = 0;
      pixels[y * width + x] = value * scale;
    }
  auto output = OIIO::ImageOutput::create(filename.string());
  motion_require(bool(output), "Cannot create iris test image");
  OIIO::ImageSpec spec(width, height, 1, OIIO::TypeDesc::FLOAT);
  spec.channelnames = {"Y"};
  motion_require(output->open(filename.string(), spec) && output->write_image(OIIO::TypeDesc::FLOAT, pixels.data()) &&
                     output->close(),
                 "Cannot write iris test image");
  // Force distinct revisions even on filesystems with coarse modification times.
  static int serial = 0;
  std::filesystem::last_write_time(filename,
                                   std::filesystem::file_time_type::clock::now() + std::chrono::seconds(++serial));
}
void json_moments(std::ostream &out, const Moments &m) {
  out << "{\"energy\":" << m.energy << ",\"centroid\":[" << m.x << ',' << m.y << "],\"covariance\":[" << m.xx << ','
      << m.xy << ',' << m.yy << "]}";
}
void write_comparison(const std::filesystem::path &filename,
                      const std::array<std::shared_ptr<const FrameEvent>, 3> &frames) {
  constexpr int scale = 4;
  const int width = frames[0]->width, height = frames[0]->height;
  double maximum = 0;
  for (const auto &frame : frames) {
    const auto values = expand_image_plane(*frame, frame->planes.front());
    for (std::size_t i = 0; i < values.size(); ++i)
      if (i % 4 < 3)
        maximum = std::max(maximum, double(values[i]));
  }
  // One shared linear exposure and display gamma; EXRs retain original radiance.
  std::vector<unsigned char> pixels(width * 3 * scale * height * scale * 3);
  for (int panel = 0; panel < 3; ++panel) {
    const auto values = expand_image_plane(*frames[panel], frames[panel]->planes.front());
    for (int y = 0; y < height * scale; ++y)
      for (int x = 0; x < width * scale; ++x)
        for (int c = 0; c < 3; ++c) {
          const auto source = (std::size_t(height - 1 - y / scale) * width + x / scale) * 4 + c;
          pixels[(std::size_t(y) * width * 3 * scale + panel * width * scale + x) * 3 + c] =
              static_cast<unsigned char>(255 * std::pow(std::clamp(double(values[source]) / maximum, 0., 1.), 1 / 2.2));
        }
  }
  auto output = OIIO::ImageOutput::create(filename.string());
  motion_require(bool(output), "Cannot create image iris comparison preview");
  OIIO::ImageSpec spec(width * 3 * scale, height * scale, 3, OIIO::TypeDesc::UINT8);
  spec.attribute("ImageDescription",
                 "CDF | Hierarchical | Hierarchical rotated 90 degrees. Shared exposure, gamma 2.2.");
  motion_require(output->open(filename.string(), spec) && output->write_image(OIIO::TypeDesc::UINT8, pixels.data()) &&
                     output->close(),
                 "Cannot write image iris comparison preview");
}
} // namespace

void image_iris_render_contracts(const std::string &device, const std::string &output) {
  const auto destination = std::filesystem::absolute(output);
  std::filesystem::create_directories(destination);
  int checks = 0, rejections = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    motion_require(value, message);
  };
  SessionConfig config;
  config.device_id = device;
  config.width = 64;
  config.height = 64;
  config.samples = 64;
  config.threads = 2;
  config.interactive = true;
  config.persistent_updates = true;
  config.collect_diagnostics = true;
  RenderSession session(config);
  SceneUpdate initial;
  initial.background = Float3{.2f, .4f, .8f};
  initial.camera = CameraData{};
  initial.camera->aperture_size = .65f;
  initial.camera->focus_distance = 2.5f;
  auto baseline = motion_wait(session, session.apply(initial));
  auto camera = *initial.camera;
  const auto mask = destination / "iris-live.exr";
  write_mask(mask, 0);
  camera.image_iris = mask.string();
  camera.image_iris_method = 1;
  if (!capabilities().image_iris) {
    bool rejected = false;
    try {
      SceneUpdate update;
      update.camera = camera;
      session.apply(update);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == baseline->generation,
            "Disabled image iris was silently accepted");
    session.close();
    std::cout << "{\"result\":\"passed\",\"feature_enabled\":false,\"checks\":" << checks
              << ",\"analytic_render\":true,\"unsupported_rejected\":true}\n";
    return;
  }
  check_progressive_camera_lookup_tables();
  const auto render = [&](const CameraData &value, int samples = 64, std::optional<bool> motion_blur = {}) {
    SceneUpdate update;
    update.camera = value;
    update.motion_blur = motion_blur;
    RenderSettings settings;
    settings.samples = samples;
    settings.adaptive_sampling = false;
    settings.clamp_indirect = 0;
    update.rendering = settings;
    return motion_wait(session, session.apply(update));
  };
  const auto check_uniform = [&](const FrameEvent &frame) {
    const auto pixels = expand_image_plane(frame, frame.planes.front());
    for (std::size_t i = 0; i < pixels.size(); i += 4)
      for (int c = 0; c < 3; ++c)
        require(std::abs(pixels[i + c] - std::array<float, 3>{.2f, .4f, .8f}[c]) < 2e-5f,
                "Normalized iris changed constant-field brightness");
  };
  auto cdf_flat = render(camera);
  check_uniform(*cdf_flat);
  const auto first = session.status();
  require(first.image_iris_preparations == 1 && first.image_iris_table_bytes > 0,
          "First image iris was not prepared once");
  camera.image_iris_method = 2;
  check_uniform(*render(camera));
  require(session.status().image_iris_preparations == 1, "Switching iris sampling rebuilt its image");
  write_mask(mask, 0, .1f);
  check_uniform(*render(camera));
  require(session.status().image_iris_preparations == 2, "Same-path iris change did not prepare new data");
  write_mask(mask, 1);
  SceneUpdate witness;
  witness.background = Float3{};
  MaterialData emission;
  emission.key = "iris/emission";
  emission.kind = MaterialKind::Emission;
  emission.color = {1, 1, 1};
  witness.materials = {emission};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "iris/target";
  mesh->revision = 1;
  mesh->materials = {emission.key};
  mesh->positions = {{-.075f, -.075f, 0}, {.075f, -.075f, 0}, {.075f, .075f, 0}, {-.075f, .075f, 0}};
  mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  witness.meshes = {mesh};
  witness.objects = {{"iris/object", mesh->key}};
  witness.camera = camera;
  motion_wait(session, session.apply(witness));
  const auto converted = session.status().geometry_conversions, prepared = session.status().image_iris_preparations;
  std::array<std::shared_ptr<const FrameEvent>, 2> high, low;
  std::array<Moments, 2> measured;
  std::array<double, 2> seconds{}, noise{};
  for (int method = 1; method <= 2; ++method) {
    camera.image_iris_method = method;
    low[method - 1] = render(camera, 64);
    const auto start = Clock::now();
    high[method - 1] = render(camera, 2048);
    seconds[method - 1] = std::chrono::duration<double>(Clock::now() - start).count();
    measured[method - 1] = moments(*high[method - 1]);
    noise[method - 1] = image_difference(*low[method - 1], *high[method - 1]);
    session.write_exr((destination / (method == 1 ? "bokeh-cdf.exr" : "bokeh-hierarchical.exr")).string());
  }
  require(std::abs(measured[0].energy - measured[1].energy) < .08 * measured[0].energy,
          "CDF and hierarchy disagree on integrated brightness");
  require(std::hypot(measured[0].x - measured[1].x, measured[0].y - measured[1].y) < .6,
          "CDF and hierarchy disagree on bokeh position");
  require(std::abs(measured[0].xx - measured[1].xx) < 2 && std::abs(measured[0].yy - measured[1].yy) < 2,
          "CDF and hierarchy disagree on bokeh shape");
  const auto expected_lens = aperture_centroid(mask);
  const auto check_orientation = [&](const Moments &observed, float focus) {
    // Independent thin-lens projection of the aperture's exact first moment.
    // The target is centered five world units from the camera. Engine raster
    // Y increases upward; input image row zero is its top edge.
    const double scale =
        camera.aperture_size * (1.0 / focus - 1.0 / 5) * config.width / (2 * std::tan(camera.short_axis_fov / 2));
    require(std::abs(observed.x - ((config.width - 1) * .5 + scale * expected_lens[0])) < .7 &&
                std::abs(observed.y - ((config.height - 1) * .5 + scale * expected_lens[1])) < .7,
            "Asymmetric iris orientation disagrees with independent thin-lens projection");
  };
  check_orientation(measured[0], camera.focus_distance);
  check_orientation(measured[1], camera.focus_distance);
  camera.focus_distance = 10;
  const auto foreground = render(camera, 2048);
  check_orientation(moments(*foreground), camera.focus_distance);
  session.write_exr((destination / "bokeh-foreground.exr").string());
  camera.focus_distance = 2.5f;
  camera.aperture_rotation = std::numbers::pi_v<float> / 2;
  auto rotated = render(camera, 1024);
  const auto rotated_moments = moments(*rotated);
  session.write_exr((destination / "bokeh-rotated.exr").string());
  write_comparison(destination / "bokeh-comparison.png", {high[0], high[1], rotated});
  require(std::abs(rotated_moments.xx - measured[1].yy) < 3 && std::abs(rotated_moments.yy - measured[1].xx) < 3,
          "Lens rotation did not rotate the image aperture");
  camera.focus_distance = 3;
  render(camera);
  camera.aperture_ratio = 1.3f;
  render(camera);
  require(session.status().geometry_conversions == converted, "Image lens edits converted unchanged geometry");
  require(session.status().image_iris_preparations == prepared, "Rotation, focus or ratio rebuilt the image resource");

  const auto lookup_copies = [&]() {
    const auto diagnostics = session.diagnostics();
    const auto copies = diagnostics.buffer_copy_requests.find("lookup_table");
    return copies == diagnostics.buffer_copy_requests.end() ? 0 : copies->second.calls;
  };
  auto edited = camera;
  edited.motion = linear_transform_motion(edited.transform);
  edited.sampling.shutter_curve = {1, 0};
  const auto early = moments(*render(edited, 256, true));
  require(lookup_copies() > 0, "Enabling shutter blur did not upload a lookup table");
  edited.sampling.shutter_curve = {0, 1};
  const auto late = moments(*render(edited, 256));
  require(lookup_copies() > 0 && std::abs(early.x - late.x) > 2,
          "An authored shutter curve did not upload and change motion exposure");

  edited.motion.clear();
  render(edited, 256);
  for (int method : {1, 2}) {
    edited.image_iris_method = method;
    const auto before = moments(*render(edited, 256));
    require(lookup_copies() == 0, "Switching the iris sampler recopied the shared lookup table");
    edited.transform[3] += method == 1 ? .25f : -.25f;
    const auto after = moments(*render(edited, 256));
    require(lookup_copies() == 0 && std::abs(before.x - after.x) > 2,
            "A pose-only render failed to move the witness or recopied the shared lookup table");
  }

  edited.orthographic = true;
  edited.aperture_size = 0;
  const auto ortho = moments(*render(edited, 256));
  require(lookup_copies() == 0, "Switching to orthographic recopied the shared lookup table");
  edited.ortho_scale *= 2;
  const auto wider = moments(*render(edited, 256));
  require(lookup_copies() == 0 && ortho.energy / wider.energy > 3 && ortho.energy / wider.energy < 5,
          "An orthographic viewplane edit failed to change magnification or recopied the shared lookup table");
  require(session.status().image_iris_preparations == prepared, "Camera edits rebuilt the image iris resource");
  render(camera, 64, false);

  const auto begin_busy = [&]() {
    SceneUpdate busy;
    busy.camera = camera;
    RenderSettings long_render;
    long_render.samples = 1000000;
    busy.rendering = long_render;
    const auto generation = session.apply(busy);
    const auto deadline = Clock::now() + std::chrono::seconds(10);
    while (session.status().state != SessionState::Rendering && Clock::now() < deadline)
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    require(session.status().state == SessionState::Rendering, "IPR did not remain active for image repair test");
    return generation;
  };
  // A valid same-path edit must also replace an actively tracing generation.
  const auto active_generation = begin_busy();
  const auto active_preparations = session.status().image_iris_preparations;
  write_mask(mask, 2);
  require(session.textures_changed(), "Valid same-path iris edit was not detected during IPR");
  const auto changed = render(camera, 64);
  require(changed->generation > active_generation &&
              session.status().image_iris_preparations == active_preparations + 1 && lookup_copies() > 0,
          "Active IPR iris edit did not rebuild exactly once and upload the new table");
  const auto changed_moments = moments(*changed);
  require(changed_moments.yy > 2 * changed_moments.xx, "Active IPR image replacement did not change bokeh shape");
  // Exercise file changes and rejection while the persistent IPR generation is tracing.
  for (int fault = 0; fault < 3; ++fault) {
    const auto generation = begin_busy();
    if (fault == 0)
      std::filesystem::remove(mask);
    if (fault == 1)
      write_mask(mask, 3);
    if (fault == 2) {
      std::ofstream broken(mask, std::ios::binary | std::ios::trunc);
      broken << "This is deliberately not an image";
    }
    require(session.textures_changed(), "Iris file damage was not detected by the IPR watcher");
    bool rejected = false;
    try {
      SceneUpdate bad;
      bad.camera = camera;
      session.apply(bad);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    ++rejections;
    require(rejected && session.status().generation == generation && session.status().state != SessionState::Failed,
            "Invalid iris replaced or failed the accepted IPR scene");
    write_mask(mask, fault % 2 ? 1 : 2);
    require(session.textures_changed(), "Repair of the rejected iris was not detected");
    auto repaired = render(camera, 64);
    require(repaired->complete && repaired->generation > generation,
            "Image repair did not produce a completed replacement frame");
    require(!session.textures_changed(), "Accepted iris repair remained permanently dirty");
  }
  require(session.status().geometry_conversions == converted, "Image repair converted unchanged geometry");
  const auto final = session.status();
  session.close();
  require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
          "Image iris session retained renderer allocations");
  std::ofstream report(destination / "comparison.json");
  report << std::setprecision(10) << "{\"samples\":2048,\"width\":64,\"height\":64,\"cdf\":";
  json_moments(report, measured[0]);
  report << ",\"hierarchical\":";
  json_moments(report, measured[1]);
  report << ",\"render_seconds\":[" << seconds[0] << ',' << seconds[1] << "],\"low_sample_absolute_difference\":["
         << noise[0] << ',' << noise[1]
         << "],\"method_mean_absolute_difference\":" << image_difference(*high[0], *high[1])
         << ",\"preparations\":" << final.image_iris_preparations << ",\"table_bytes\":" << final.image_iris_table_bytes
         << ",\"preparation_seconds\":" << final.image_iris_preparation_seconds << "}\n";
  std::cout << "{\"result\":\"passed\",\"feature_enabled\":true,\"checks\":" << checks
            << ",\"image_comparisons\":2,\"rejected_inputs\":" << rejections
            << ",\"camera_lookup_tables_reused\":true,\"persistent_ipr\":true,\"closed_owned_bytes\":0,"
               "\"closed_device_bytes\":0}\n";
}
} // namespace cycles_houdini::harness
