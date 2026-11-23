// SPDX-License-Identifier: Apache-2.0
#include "motion_fixture.h"
#include "cycles_houdini/image_iris.h"
#include <OpenImageIO/imageio.h>
#include <array>
#include <fstream>
#include <iomanip>

namespace cycles_houdini::harness {
namespace {
using Clock = std::chrono::steady_clock;
constexpr int mask_side = 33;
using Color = std::array<double, 3>;
struct ColorMoments {
  Color energy{}, x{}, y{};
};
struct Mask {
  int channels = 3;
  std::vector<float> pixels;
  Color mass{}, x{}, y{};
};
Mask make_mask(int kind) {
  Mask result;
  result.channels = kind == 2 ? 1 : kind == 3 ? 4 : 3;
  result.pixels.resize(mask_side * mask_side * result.channels);
  for (int y = 0; y < mask_side; ++y)
    for (int x = 0; x < mask_side; ++x) {
      const float u = float(x) / (mask_side - 1), v = float(y) / (mask_side - 1);
      Color rgb{.2, .5, .9};
      if (kind == 1 || kind == 2)
        rgb = {.1 + .8 * u, .1 + .8 * u, .1 + .8 * u};
      if (kind == 4)
        rgb = {.02 + (u < .35 && v > .25 && v < .75 ? 1. : 0.), .02 + (v < .35 && u > .25 && u < .75 ? 1. : 0.),
               .02 + (u > .65 && v > .25 && v < .75 ? 1. : 0.)};
      if (kind == 5)
        rgb = {0, 0, 0};
      const auto index = (y * mask_side + x) * result.channels;
      for (int c = 0; c < std::min(result.channels, 3); ++c)
        result.pixels[index + c] = float(rgb[c]);
      if (result.channels == 4)
        result.pixels[index + 3] = .1f + .9f * u;
    }
  // Exact integrals and first moments of the bilinear transmission field.
  for (int y = 0; y < mask_side - 1; ++y)
    for (int x = 0; x < mask_side - 1; ++x)
      for (int c = 0; c < 3; ++c) {
        const auto value = [&](int px, int py) {
          return double(result.pixels[(py * mask_side + px) * result.channels + (result.channels == 1 ? 0 : c)]);
        };
        const double a = value(x, y), b = value(x + 1, y), d = value(x + 1, y + 1), e = value(x, y + 1),
                     mass = (a + b + d + e) / 4;
        result.mass[c] += mass;
        result.x[c] += 2 * (x * mass + (a + e + 2 * b + 2 * d) / 12) / (mask_side - 1) - mass;
        result.y[c] += mass - 2 * (y * mass + (a + b + 2 * e + 2 * d) / 12) / (mask_side - 1);
      }
  for (int c = 0; c < 3; ++c)
    if (result.mass[c] > 0) {
      result.x[c] /= result.mass[c];
      result.y[c] /= result.mass[c];
    }
  return result;
}
void write_mask(const std::filesystem::path &filename, const Mask &mask) {
  auto output = OIIO::ImageOutput::create(filename.string());
  motion_require(bool(output), "Cannot create colored iris fixture");
  OIIO::ImageSpec spec(mask_side, mask_side, mask.channels, OIIO::TypeDesc::FLOAT);
  spec.channelnames = mask.channels == 1   ? std::vector<std::string>{"Y"}
                      : mask.channels == 4 ? std::vector<std::string>{"R", "G", "B", "A"}
                                           : std::vector<std::string>{"R", "G", "B"};
  if (mask.channels == 4)
    spec.alpha_channel = 3;
  spec.attribute("oiio:UnassociatedAlpha", 1);
  motion_require(output->open(filename.string(), spec) &&
                     output->write_image(OIIO::TypeDesc::FLOAT, mask.pixels.data()) && output->close(),
                 "Cannot write colored iris fixture");
  static int revision = 0;
  std::filesystem::last_write_time(filename,
                                   std::filesystem::file_time_type::clock::now() + std::chrono::seconds(++revision));
}
ColorMoments moments(const FrameEvent &frame) {
  ColorMoments result;
  const auto pixels = expand_image_plane(frame, frame.planes.front());
  for (int y = 0; y < frame.height; ++y)
    for (int x = 0; x < frame.width; ++x)
      for (int c = 0; c < 3; ++c) {
        const double value = pixels[(y * frame.width + x) * 4 + c];
        motion_require(std::isfinite(value) && value >= 0, "Colored iris emitted non-finite or negative radiance");
        result.energy[c] += value;
        result.x[c] += x * value;
        result.y[c] += y * value;
      }
  for (int c = 0; c < 3; ++c) {
    motion_require(result.energy[c] > 0, "Colored iris lost a transmission channel");
    result.x[c] /= result.energy[c];
    result.y[c] /= result.energy[c];
  }
  return result;
}
void json_color(std::ostream &output, const Color &value) {
  output << '[' << value[0] << ',' << value[1] << ',' << value[2] << ']';
}
void json_moments(std::ostream &output, const ColorMoments &value) {
  output << "{\"energy\":";
  json_color(output, value.energy);
  output << ",\"x\":";
  json_color(output, value.x);
  output << ",\"y\":";
  json_color(output, value.y);
  output << '}';
}
void preview(const std::filesystem::path &filename, const FrameEvent &a, const FrameEvent &b) {
  constexpr int scale = 4;
  const int width = a.width, height = a.height;
  std::vector<unsigned char> bytes(width * 2 * scale * height * scale * 3);
  std::array<std::vector<float>, 2> frames{expand_image_plane(a, a.planes.front()),
                                           expand_image_plane(b, b.planes.front())};
  double maximum = 0;
  for (const auto &pixels : frames)
    for (std::size_t i = 0; i < pixels.size(); ++i)
      if (i % 4 < 3)
        maximum = std::max(maximum, double(pixels[i]));
  motion_require(maximum > 0, "Cannot display an empty colored bokeh");
  for (int panel = 0; panel < 2; ++panel)
    for (int y = 0; y < height * scale; ++y)
      for (int x = 0; x < width * scale; ++x)
        for (int c = 0; c < 3; ++c) {
          const auto source = ((height - 1 - y / scale) * width + x / scale) * 4 + c;
          bytes[(std::size_t(y) * width * 2 * scale + panel * width * scale + x) * 3 + c] = static_cast<unsigned char>(
              255 * std::pow(std::clamp(double(frames[panel][source]) / maximum, 0., 1.), 1 / 2.2));
        }
  auto output = OIIO::ImageOutput::create(filename.string());
  OIIO::ImageSpec spec(width * 2 * scale, height * scale, 3, OIIO::TypeDesc::UINT8);
  spec.attribute("ImageDescription",
                 "Colored image iris: CDF | Hierarchical. Shared linear exposure and display gamma 2.2.");
  motion_require(output && output->open(filename.string(), spec) &&
                     output->write_image(OIIO::TypeDesc::UINT8, bytes.data()) && output->close(),
                 "Cannot write colored iris preview");
}
} // namespace

void image_iris_color_render_contracts(const std::string &device, const std::string &output) {
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
  config.passes = {"combined", "mist"};
  config.cryptomatte.object = true;
  config.cryptomatte.levels = 2;
  RenderSession session(config);
  SceneUpdate initial;
  initial.background = Float3{1, 1, 1};
  initial.camera = CameraData{};
  initial.camera->aperture_size = .65f;
  initial.camera->focus_distance = 2.5f;
  const auto baseline = motion_wait(session, session.apply(initial));
  auto camera = *initial.camera;
  const auto filename = destination / "iris-color-live.exr";
  write_mask(filename, make_mask(0));
  camera.image_iris = filename.string();
  camera.image_iris_method = 1;
  camera.image_iris_color = true;
  if (!capabilities().image_iris) {
    bool rejected = false;
    try {
      SceneUpdate edit;
      edit.camera = camera;
      session.apply(edit);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected && session.status().generation == baseline->generation,
            "Unavailable colored iris was accepted or replaced the scene");
    session.close();
    std::cout << "{\"result\":\"passed\",\"feature_enabled\":false,\"unsupported_rejected\":true}\n";
    return;
  }
  const auto render = [&](int samples = 64) {
    SceneUpdate edit;
    edit.camera = camera;
    RenderSettings settings;
    settings.samples = samples;
    settings.adaptive_sampling = false;
    settings.clamp_indirect = 0;
    edit.rendering = settings;
    return motion_wait(session, session.apply(edit));
  };
  const auto uniform = [&](const FrameEvent &frame, const Color &expected) {
    const auto pixels = expand_image_plane(frame, frame.planes.front());
    for (std::size_t i = 0; i < pixels.size(); i += 4) {
      for (int c = 0; c < 3; ++c)
        require(std::isfinite(pixels[i + c]) && std::abs(pixels[i + c] - expected[c]) < 3e-4,
                "Colored iris uniform-field transmission differs from analytic RGB/luminance");
      require(std::abs(pixels[i + 3] - 1) < 1e-6, "Colored iris changed opaque-world alpha");
    }
  };
  const double luminance = .2126 * .2 + .7152 * .5 + .0722 * .9;
  const Color tint{.2 / luminance, .5 / luminance, .9 / luminance};
  uniform(*render(), tint);
  const auto first = session.status();
  camera.image_iris_color = false;
  uniform(*render(), {1, 1, 1});
  camera.image_iris_color = true;
  uniform(*render(), tint);
  camera.image_iris_method = 2;
  uniform(*render(), tint);
  require(session.status().image_iris_preparations == first.image_iris_preparations,
          "Color or method toggle rebuilt unchanged iris data");
  camera.orthographic = true;
  uniform(*render(), tint);
  camera.orthographic = false;
  camera.panorama = PanoramaSettings{};
  uniform(*render(), tint);
  camera.panorama.reset();
  // Neutral RGB, grayscale, and RGB with a varying alpha mask preserve their expected exposure.
  for (int kind : {1, 2, 3}) {
    write_mask(filename, make_mask(kind));
    uniform(*render(), kind == 3 ? tint : Color{1, 1, 1});
  }
  const auto mask = make_mask(4);
  write_mask(filename, mask);
  camera.image_iris_method = 1;
  SceneUpdate witness;
  witness.background = Float3{};
  witness.transparent_background = true;
  witness.camera = camera;
  MaterialData emission;
  emission.key = "iris-color/emission";
  emission.kind = MaterialKind::Emission;
  emission.color = {1, 1, 1};
  witness.materials = {emission};
  auto mesh = std::make_shared<MeshData>();
  mesh->key = "iris-color/target";
  mesh->revision = 1;
  mesh->materials = {emission.key};
  mesh->positions = {{-.075f, -.075f, 0}, {.075f, -.075f, 0}, {.075f, .075f, 0}, {-.075f, .075f, 0}};
  mesh->triangles = {{0, 1, 2}, {0, 2, 3}};
  witness.meshes = {mesh};
  witness.objects = {{"iris-color/object", mesh->key}};
  motion_wait(session, session.apply(witness));
  const auto converted = session.status().geometry_conversions, prepared = session.status().image_iris_preparations;
  std::array<std::shared_ptr<const FrameEvent>, 2> frames;
  std::array<ColorMoments, 2> measured;
  const auto check_projection = [&](const ColorMoments &value) {
    const double scale = camera.aperture_size * (1. / camera.focus_distance - 1. / 5) * config.width /
                         (2 * std::tan(camera.short_axis_fov / 2));
    for (int c = 0; c < 3; ++c) {
      require(std::abs(value.x[c] - ((config.width - 1) * .5 + scale * mask.x[c])) < .85 &&
                  std::abs(value.y[c] - ((config.height - 1) * .5 + scale * mask.y[c])) < .85,
              "Color-channel bokeh displacement disagrees with independent thin-lens integration");
      require(std::abs((value.energy[c] / value.energy[1]) / (mask.mass[c] / mask.mass[1]) - 1) < .12,
              "Bokeh channel energy ratios disagree with aperture transmission");
    }
  };
  for (int method = 1; method <= 2; ++method) {
    camera.image_iris_method = method;
    frames[method - 1] = render(2048);
    measured[method - 1] = moments(*frames[method - 1]);
    check_projection(measured[method - 1]);
    session.write_exr(
        (destination / (method == 1 ? "colored-bokeh-cdf.exr" : "colored-bokeh-hierarchical.exr")).string());
  }
  for (int c = 0; c < 3; ++c) {
    require(std::hypot(measured[0].x[c] - measured[1].x[c], measured[0].y[c] - measured[1].y[c]) < .75,
            "CDF and hierarchy disagree on color-channel bokeh position");
    require(std::abs(measured[0].energy[c] / measured[1].energy[c] - 1) < .12,
            "CDF and hierarchy disagree on color-channel energy");
  }
  preview(destination / "colored-bokeh-comparison.png", *frames[0], *frames[1]);
  // The exact same sampled aperture positions must keep geometric coverage alpha independent of RGB weighting.
  camera.image_iris_color = false;
  const auto gray = render(2048);
  const auto color_pixels = expand_image_plane(*frames[1], frames[1]->planes.front()),
             gray_pixels = expand_image_plane(*gray, gray->planes.front());
  for (std::size_t i = 3; i < gray_pixels.size(); i += 4)
    require(std::abs(color_pixels[i] - gray_pixels[i]) < 1e-6, "Color transmission changed geometric coverage alpha");
  bool checked_crypto = false, checked_mist = false;
  for (const auto &plane : frames[1]->planes) {
    if (plane.pass != "mist" && plane.pass.rfind("CryptoObject", 0) != 0)
      continue;
    const auto reference = std::find_if(gray->planes.begin(), gray->planes.end(),
                                        [&](const ImagePlane &value) { return value.pass == plane.pass; });
    require(reference != gray->planes.end() && reference->channels == plane.channels,
            "Color toggle lost a coverage data pass");
    const auto colored = expand_image_plane(*frames[1], plane), neutral = expand_image_plane(*gray, *reference);
    if (plane.pass == "mist") {
      checked_mist = true;
      for (std::size_t i = 0; i < colored.size(); ++i)
        require(std::isfinite(colored[i]) && std::abs(colored[i] - neutral[i]) < 1e-6,
                "Color transmission changed opaque-surface Mist weights");
    } else {
      checked_crypto = true;
      require(plane.channels == 4, "Cryptomatte coverage plane has an unexpected layout");
      // ID floats are opaque bit patterns, potentially NaNs; only G/A carry coverage.
      for (std::size_t i = 0; i < colored.size(); i += 4) {
        const double a = colored[i + 1] + colored[i + 3], b = neutral[i + 1] + neutral[i + 3];
        require(std::isfinite(a) && std::abs(a - b) < 1e-6,
                "Color transmission changed Cryptomatte object coverage weights");
      }
    }
  }
  require(checked_crypto && checked_mist, "Colored iris coverage test did not produce Cryptomatte and Mist passes");
  camera.image_iris_color = true;
  camera.focus_distance = 10;
  check_projection(moments(*render(2048)));
  session.write_exr((destination / "colored-bokeh-foreground.exr").string());
  camera.focus_distance = 5;
  const auto focused = moments(*render(2048));
  for (int c = 0; c < 3; ++c)
    require(std::hypot(focused.x[c] - focused.x[1], focused.y[c] - focused.y[1]) < .3,
            "RGB aperture channels did not converge in focus");
  camera.focus_distance = 2.5f;
  require(session.status().image_iris_preparations == prepared && session.status().geometry_conversions == converted,
          "Color, focus or method edit rebuilt the iris or geometry");
  const auto busy = [&]() {
    SceneUpdate edit;
    edit.camera = camera;
    RenderSettings settings;
    settings.samples = 1000000;
    settings.adaptive_sampling = false;
    edit.rendering = settings;
    const auto generation = session.apply(edit);
    const auto deadline = Clock::now() + std::chrono::seconds(10);
    while (session.status().state != SessionState::Rendering && Clock::now() < deadline)
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    require(session.status().state == SessionState::Rendering, "Colored iris repair fixture was not actively tracing");
    return generation;
  };
  const auto active = busy();
  const auto preparations = session.status().image_iris_preparations;
  write_mask(filename, make_mask(0));
  require(session.textures_changed(), "Live RGB image edit was not detected");
  require(render()->generation > active && session.status().image_iris_preparations == preparations + 1,
          "Live RGB image edit did not replace tracing data exactly once");
  for (int fault = 0; fault < 3; ++fault) {
    const auto generation = busy();
    if (fault == 0)
      std::filesystem::remove(filename);
    if (fault == 1)
      write_mask(filename, make_mask(5));
    if (fault == 2) {
      std::ofstream invalid(filename, std::ios::binary | std::ios::trunc);
      invalid << "Deliberately damaged colored iris";
    }
    require(session.textures_changed(), "Damaged RGB image was not detected while tracing");
    bool rejected = false;
    try {
      SceneUpdate edit;
      edit.camera = camera;
      session.apply(edit);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    ++rejections;
    require(rejected && session.status().generation == generation && session.status().state != SessionState::Failed,
            "Invalid RGB image destroyed the accepted live scene");
    write_mask(filename, make_mask(fault % 2 ? 3 : 4));
    require(session.textures_changed(), "Same-path colored image repair was not detected");
    require(render()->generation > generation && !session.textures_changed(),
            "RGB repair did not finish a clean replacement frame");
  }
  require(session.status().geometry_conversions == converted, "RGB image repair converted unchanged geometry");
  // Cycles stores a scalar coverage/transparency alpha. Camera color transmission
  // must not make an achromatic transparent surface or full holdout opaque.
  write_mask(filename, make_mask(0));
  camera.image_iris_method = 1;
  camera.image_iris_color = false;
  auto alpha_mesh = std::make_shared<MeshData>(*mesh);
  alpha_mesh->revision = 2;
  alpha_mesh->positions = {{-20, -20, 0}, {20, -20, 0}, {20, 20, 0}, {-20, 20, 0}};
  SceneUpdate alpha_geometry;
  alpha_geometry.meshes = {alpha_mesh};
  alpha_geometry.camera = camera;
  motion_wait(session, session.apply(alpha_geometry));
  std::array<std::array<double, 2>, 3> alpha_results{};
  for (int kind = 0; kind < 3; ++kind) {
    MaterialData material;
    material.key = emission.key;
    material.kind = MaterialKind::Graph;
    ShaderGraphData graph;
    if (kind == 2) {
      graph.nodes = {{"surface", "cycles/holdout/1", {}}};
      graph.surface = {"surface", "holdout"};
    } else {
      graph.nodes = {
          {"surface",
           "cycles/transparent_bsdf/1",
           {{"color", kind == 0 ? std::array<float, 3>{.4f, .4f, .4f} : std::array<float, 3>{.2f, .5f, .8f}}}}};
      graph.surface = {"surface", "BSDF"};
    }
    material.graph = graph;
    SceneUpdate shader;
    shader.materials = {material};
    motion_wait(session, session.apply(shader));
    for (int color = 0; color < 2; ++color) {
      camera.image_iris_color = bool(color);
      const auto frame = render(64);
      const auto pixels = expand_image_plane(*frame, frame->planes.front());
      const double expected = kind == 2   ? 0
                              : kind == 0 ? .6
                              : color     ? 1 - (.2 * .2 + .5 * .5 + .9 * .8) / (.2 + .5 + .9)
                                          : .5;
      double sum = 0;
      for (std::size_t i = 3; i < pixels.size(); i += 4) {
        require(std::isfinite(pixels[i]) && std::abs(pixels[i] - expected) < 3e-4,
                "Holdout or transparent material alpha differs from normalized camera-weighted transmission");
        sum += pixels[i];
      }
      alpha_results[kind][color] = sum / (config.width * config.height);
    }
    if (kind != 1)
      require(std::abs(alpha_results[kind][0] - alpha_results[kind][1]) < 1e-6,
              "Color iris changed neutral material transparency or full holdout coverage");
  }
  const auto final = session.status();
  session.close();
  require(session.status().owned_geometry_bytes == 0 && session.status().device_allocation_bytes == 0,
          "Colored image iris retained renderer allocations after close");
  std::ofstream report(destination / "comparison.json");
  report << std::setprecision(10) << "{\"samples\":2048,\"analytic_uniform_tint\":";
  json_color(report, tint);
  report << ",\"cdf\":";
  json_moments(report, measured[0]);
  report << ",\"hierarchical\":";
  json_moments(report, measured[1]);
  report << ",\"neutral_material_alpha\":[" << alpha_results[0][0] << ',' << alpha_results[0][1]
         << "],\"colored_material_alpha\":[" << alpha_results[1][0] << ',' << alpha_results[1][1]
         << "],\"holdout_alpha\":[" << alpha_results[2][0] << ',' << alpha_results[2][1]
         << "],\"preparations\":" << final.image_iris_preparations
         << ",\"table_bytes\":" << final.image_iris_table_bytes << "}\n";
  std::cout << "{\"result\":\"passed\",\"feature_enabled\":true,\"checks\":" << checks
            << ",\"image_comparisons\":2,\"rejected_inputs\":" << rejections
            << ",\"persistent_ipr\":true,\"coverage_alpha_unchanged\":true,\"crypto_mist_coverage_unchanged\":true,"
               "\"closed_owned_bytes\":0,\"closed_device_bytes\":0}\n";
}
} // namespace cycles_houdini::harness
