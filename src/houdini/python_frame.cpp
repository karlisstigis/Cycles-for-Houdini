// SPDX-License-Identifier: Apache-2.0
#include "python_binding_state.h"
#include <pybind11/stl.h>
#include <chrono>
#include <cmath>
#include <limits>
#include "../core/color_internal.h"
#include "../core/exr_validation.h"
#include "../harness/compact_frame_fixture.h"

namespace cycles_houdini::python_detail {
py::object frame_capsule(FramePtr image) {
  if (!image)
    return py::none();
  auto value = std::make_unique<FramePtr>(std::move(image));
  py::capsule result(value.get(), "cycles_houdini.Frame", destroy_owned<FramePtr>);
  value.release();
  return result;
}

FramePtr read_image(py::handle value, std::uint64_t after) {
  if (value && py::isinstance<py::capsule>(value)) {
    auto capsule = py::reinterpret_borrow<py::capsule>(value);
    if (capsule.name() && std::strcmp(capsule.name(), "cycles_houdini.Frame") == 0) {
      const auto image = *capsule_pointer<FramePtr>(value, "cycles_houdini.Frame", "Invalid frame");
      return image && image->sequence > after ? image : FramePtr{};
    }
  }
  auto &h = handle(value);
  if (!h.session)
    throw std::invalid_argument("Capture handle has no rendering session");
  return h.session->frame(after);
}

py::memoryview frame_pixels_view(const FramePtr &frame, const ch::ImagePlane &plane) {
  if (plane.pixels.size() > std::size_t(PY_SSIZE_T_MAX) / sizeof(float))
    throw std::invalid_argument("Display plane exceeds buffer allocation limits");
  auto pixels = std::make_unique<FramePixels>();
  pixels->owner = frame;
  pixels->data = plane.pixels.data();
  pixels->size = py::ssize_t(plane.pixels.size() * sizeof(float));
  auto owner = py::cast(pixels.release(), py::return_value_policy::take_ownership);
  return py::memoryview(owner);
}

py::bytes plane_pixels(const ch::FrameEvent &image, const ch::ImagePlane &plane) {
  // Explicit raw-frame requests retain their full logical raster contract.
  // The fast display/transport routes never materialize this expansion.
  ch::image_plane_size(image, plane);
  const auto size = std::size_t(image.width) * image.height * plane.channels * sizeof(float);
  py::bytes result = py::bytes(static_cast<const char *>(nullptr), Py_ssize_t(size));
  auto *target = reinterpret_cast<float *>(unpublished_bytes_data(result));
  {
    ReleaseGIL release;
    ch::copy_image_plane_rows(image, plane, 0, image.height, target);
  }
  return result;
}

py::object frame(py::handle capsule_arg, py::handle after_arg) {
  py::handle capsule;
  unsigned long long after = 0;
  capsule = capsule_arg;
  after = unsigned_argument_mask(after_arg);
  auto image = read_image(capsule, after);
  if (!image)
    return py::none();
  py::dict planes = py::dict();
  for (const auto &p : image->planes) {
    py::dict value = py::dict(py::arg("channels") = py::int_(p.channels), py::arg("pixels") = plane_pixels(*image, p));
    planes[p.pass.c_str()] = value;
  }
  return py::dict(
      py::arg("samples") = py::int_(image->samples), py::arg("generation") = py::int_(image->generation),
      py::arg("sequence") = py::int_(image->sequence), py::arg("x") = py::int_(image->x),
      py::arg("y") = py::int_(image->y), py::arg("width") = py::int_(image->width),
      py::arg("height") = py::int_(image->height), py::arg("full_width") = py::int_(image->full_width),
      py::arg("full_height") = py::int_(image->full_height),
      py::arg("rendered_width") = py::int_(image->rendered_width),
      py::arg("rendered_height") = py::int_(image->rendered_height),
      py::arg("published_seconds") = py::float_(image->published_seconds),
      py::arg("complete") = py::bool_(image->complete), py::arg("planes") = planes,
      py::arg("working_space") = py::str(image->color_configuration->policy().working_space.c_str()),
      py::arg("color_configuration") = py::str(image->color_configuration->policy().configuration_id.c_str()),
      py::arg("render_color_policy") = py::str(image->color_configuration->policy().render_policy_id.c_str()),
      py::arg("color_epoch") = py::int_(image->color_epoch), py::arg("denoiser") = py::str(image->denoiser.c_str()),
      py::arg("denoise_device_id") = py::str(image->denoise_device_id.c_str()));
}

py::dict pass_value(const ch::PassDescriptor &pass) {
  py::list channels = py::list(0);
  for (const auto &channel : pass.channels) {
    py::str value = py::str(channel.c_str());
    channels.append(value);
  }
  return py::dict(py::arg("id") = py::str(pass.id.c_str()), py::arg("channels") = channels,
                  py::arg("storage") = py::str("float32"), py::arg("filtered") = py::bool_(pass.filtered),
                  py::arg("color") = py::bool_(pass.color),
                  py::arg("denoise_eligible") = py::bool_(pass.denoise_eligible),
                  py::arg("cryptomatte") = py::bool_(pass.cryptomatte));
}

py::object frame_info(py::handle capsule_arg, py::handle after_arg) {
  py::handle capsule;
  unsigned long long after = 0;
  capsule = capsule_arg;
  after = unsigned_argument_mask(after_arg);
  auto image = read_image(capsule, after);
  if (!image)
    return py::none();
  py::list passes = py::list(0);
  for (const auto &p : image->planes) {
    py::str name = py::str(p.pass.c_str());
    passes.append(name);
  }
  py::dict result = py::dict(
      py::arg("samples") = py::int_(image->samples), py::arg("generation") = py::int_(image->generation),
      py::arg("sequence") = py::int_(image->sequence), py::arg("x") = py::int_(image->x),
      py::arg("y") = py::int_(image->y), py::arg("width") = py::int_(image->width),
      py::arg("height") = py::int_(image->height), py::arg("full_width") = py::int_(image->full_width),
      py::arg("full_height") = py::int_(image->full_height),
      py::arg("rendered_width") = py::int_(image->rendered_width),
      py::arg("rendered_height") = py::int_(image->rendered_height),
      py::arg("published_seconds") = py::float_(image->published_seconds),
      py::arg("complete") = py::bool_(image->complete), py::arg("passes") = passes,
      py::arg("working_space") = py::str(image->color_configuration->policy().working_space.c_str()),
      py::arg("color_configuration") = py::str(image->color_configuration->policy().configuration_id.c_str()),
      py::arg("render_color_policy") = py::str(image->color_configuration->policy().render_policy_id.c_str()),
      py::arg("color_epoch") = py::int_(image->color_epoch), py::arg("denoiser") = py::str(image->denoiser.c_str()),
      py::arg("denoise_device_id") = py::str(image->denoise_device_id.c_str()));
  py::list enabled = py::list(0);
  for (const auto &pass : image->requested_passes) {
    py::str name = py::str(pass.id.c_str());
    enabled.append(name);
  }
  result["enabled_passes"] = enabled;
  py::dict metadata = py::dict();
  for (const auto &pass : image->requested_passes) {
    py::object value = pass_value(pass);
    metadata[pass.id.c_str()] = value;
  }
  result["pass_metadata"] = metadata;
  py::dict storage = py::dict();
  std::size_t bytes = 0;
  for (const auto &plane : image->planes) {
    const auto [width, height] = ch::image_plane_size(*image, plane);
    const auto size = plane.pixels.size() * sizeof(float);
    bytes += size;
    py::dict info = py::dict(py::arg("width") = py::int_(width), py::arg("height") = py::int_(height),
                             py::arg("bytes") = py::int_(size));
    storage[plane.pass.c_str()] = info;
  }
  py::int_ size = py::int_(bytes);
  result["plane_storage"] = storage;
  result["pixel_bytes"] = size;
  {
    py::float_ aspect = py::float_(image->pixel_aspect);
    result["pixel_aspect"] = aspect;
  }
  return result;
}

ch::RenderSession::OutputOptions output_options(py::handle data) {
  ch::RenderSession::OutputOptions result;
  if (!data || data.is_none())
    return result;
  if (auto v = item(data, "half_color", false))
    result.half_color = int(truth(v)) > 0;
  if (auto v = item(data, "crop_to_region", false))
    result.crop_to_region = int(truth(v)) > 0;
  if (auto v = item(data, "multipart", false))
    result.multipart = int(truth(v)) > 0;
  if (auto v = item(data, "fail_on_missing_inputs", false))
    result.fail_on_missing_inputs = int(truth(v)) > 0;
  if (auto v = item(data, "existing_files", false))
    result.existing_files = string(v);
  if (auto v = item(data, "compression", false))
    result.compression = string(v);
  if (auto v = item(data, "dwa_level", false))
    result.dwa_level = float(number(v));
  if (auto v = item(data, "pixel_aspect", false))
    result.pixel_aspect = float(number(v));
  if (auto v = item(data, "color_space", false))
    result.color_space = string(v);
  if (auto v = item(data, "color_configuration", false))
    result.color_configuration = string(v);
  if (auto v = item(data, "alpha", false))
    result.alpha = int(truth(v)) > 0;
  if (auto values = item(data, "metadata", false))
    for (auto v : sequence(values)) {
      ch::RenderSession::OutputAttribute attribute;
      attribute.name = string(item(v, "name"));
      attribute.type = string(item(v, "type"));
      const auto field = item(v, "value");
      if (attribute.type == "string")
        attribute.text = string(field);
      else if (attribute.type == "rational" || attribute.type == "int2" || attribute.type == "float2")
        for (auto n : sequence(field))
          attribute.numbers.push_back(number(n));
      else
        attribute.numbers.push_back(number(field));
      result.metadata.push_back(std::move(attribute));
    }
  if (auto values = item(data, "passes", false))
    for (auto v : sequence(values)) {
      ch::RenderSession::OutputPass selected;
      selected.source = string(item(v, "source"));
      selected.layer = string(item(v, "layer"));
      if (auto field = item(v, "precision", false))
        selected.precision = string(field);
      if (auto field = item(v, "part", false))
        selected.part = string(field);
      if (auto field = item(v, "compression", false))
        selected.compression = string(field);
      if (auto field = item(v, "dwa_level", false))
        selected.dwa_level = float(number(field));
      result.passes.push_back(std::move(selected));
    }
  return result;
}

py::object write_exr(py::handle capsule_arg, py::str path_arg, py::handle options_arg) {
  py::handle capsule;
  const char *path;
  py::handle options = nullptr;
  capsule = capsule_arg;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  if (!is_omitted(options_arg))
    options = options_arg;
  auto &h = handle(capsule);
  const auto policy = output_options(options);
  {
    ReleaseGIL release;
    h.session->write_exr(path, policy);
  }
  return py::none();
}

void inspect_existing_exr(const std::string &filename, int expected_parts, py::function validate,
                          py::object checkpoint) {
  c_string_checked(filename);
  std::function<void()> before_read;
  if (!checkpoint.is_none())
    before_read = [&] {
      py::gil_scoped_acquire acquire;
      checkpoint();
    };
  ReleaseGIL release;
  ch::inspect_existing_exr_file(
      filename, expected_parts,
      [&](const std::vector<OIIO::ImageSpec> &specs) {
        py::gil_scoped_acquire acquire;
        py::list headers;
        for (const auto &spec : specs)
          headers.append(py::dict(
              py::arg("window") = py::make_tuple(spec.x, spec.y, spec.width, spec.height, spec.full_x, spec.full_y,
                                                 spec.full_width, spec.full_height),
              py::arg("flat") =
                  !spec.deep && spec.depth == 1 && spec.z == 0 && spec.full_z == 0 && spec.full_depth == 1,
              py::arg("name") = std::string(spec.get_string_attribute("oiio:subimagename")),
              py::arg("channels") = spec.channelnames,
              py::arg("validation_version") = spec.get_int_attribute("cycles_houdini:input_validation_version", 0),
              py::arg("missing_inputs") = spec.get_int_attribute("cycles_houdini:missing_input_count", -1),
              py::arg("diagnostics") = std::string(spec.get_string_attribute("cycles_houdini:source_diagnostics"))));
        return validate(headers).cast<bool>();
      },
      before_read);
}

py::object encode_output(py::str path_arg, py::handle options_arg) {
  const char *path;
  py::handle options = nullptr;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  if (!is_omitted(options_arg))
    options = options_arg;
  const auto bytes = ch::encode_worker_output({path, output_options(options)});
  return py::bytes(reinterpret_cast<const char *>(bytes.data()), bytes.size());
}

py::object encode_preview(py::str pass_arg, py::handle focus_arg) {
  const char *pass;
  int focus = 0;
  const auto pass_storage = pass_arg.cast<std::string>();
  pass = c_string_checked(pass_storage);
  focus = truth(focus_arg);
  const auto bytes = ch::encode_worker_preview({pass, focus != 0});
  return py::bytes(reinterpret_cast<const char *>(bytes.data()), bytes.size());
}

ch::DisplaySettings read_display_settings(py::handle data) {
  ch::DisplaySettings settings;
  if (auto v = item(data, "pass", false))
    settings.pass = string(v);
  if (auto v = item(data, "display", false))
    settings.display = string(v);
  if (auto v = item(data, "view", false))
    settings.view = string(v);
  if (auto v = item(data, "exposure", false))
    settings.exposure = float(number(v));
  if (auto v = item(data, "gamma", false))
    settings.gamma = float(number(v));
  if (auto v = item(data, "raw", false))
    settings.raw = int(truth(v)) > 0;
  if (auto v = item(data, "channels", false))
    settings.channels = string(v);
  if (auto v = item(data, "backgrounds", false)) {
    for (auto entry : py::reinterpret_borrow<py::sequence>(v)) {
      const auto values = py::reinterpret_borrow<py::sequence>(entry);
      if (values.size() != 3 || settings.backgrounds.size() >= 2)
        throw std::invalid_argument("Display backgrounds are at most two RGB byte triples");
      std::array<std::uint8_t, 3> rgb{};
      for (int c = 0; c < 3; ++c) {
        const auto value = argument_integer<int>(values[c]);
        if (value < 0 || value > 255)
          throw std::invalid_argument("Display background components must be 0..255");
        rgb[c] = std::uint8_t(value);
      }
      settings.backgrounds.push_back(rgb);
    }
  }
  return settings;
}

py::object display_image(py::handle capsule_arg, py::handle data_arg) {
  py::handle capsule, data;
  capsule = capsule_arg;
  data = data_arg;
  auto image = read_image(capsule);
  if (!image)
    return py::none();
  const auto settings = read_display_settings(data);
  const auto diagnostic_value = item(data, "collect_diagnostics", false);
  const bool diagnostic = diagnostic_value.is(py::bool_(true));
  const bool compact = item(data, "compact_full_frame", false).is(py::bool_(true));
  using Clock = std::chrono::steady_clock;
  Clock::time_point converted;
  ch::DisplayTimings timings;
  ch::DisplayImage result;
  std::pair<int, int> storage;
  {
    ReleaseGIL release;
    result =
        ch::display_image_with_storage(*image, settings, diagnostic ? &timings : nullptr, compact ? &storage : nullptr);
    if (diagnostic)
      converted = Clock::now();
  }
  const double gil_seconds = diagnostic ? std::chrono::duration<double>(Clock::now() - converted).count() : 0;
  py::bytes rgba = py::bytes(static_cast<const char *>(nullptr), Py_ssize_t(result.rgba.size()));
  auto *destination = unpublished_bytes_data(rgba);
  {
    ReleaseGIL release;
    std::memcpy(destination, result.rgba.data(), result.rgba.size());
  }
  py::dict output = py::dict(
      py::arg("generation") = py::int_(result.generation), py::arg("sequence") = py::int_(result.sequence),
      py::arg("x") = py::int_(result.x), py::arg("y") = py::int_(result.y), py::arg("width") = py::int_(result.width),
      py::arg("height") = py::int_(result.height), py::arg("full_width") = py::int_(result.full_width),
      py::arg("full_height") = py::int_(result.full_height),
      py::arg("rendered_width") = py::int_(image->rendered_width),
      py::arg("rendered_height") = py::int_(image->rendered_height), py::arg("samples") = py::int_(image->samples),
      py::arg("published_seconds") = py::float_(image->published_seconds), py::arg("rgba") = rgba);
  if (compact && storage != std::pair{result.width, result.height}) {
    output["buffer_width"] = py::int_(storage.first);
    output["buffer_height"] = py::int_(storage.second);
  }
  {
    py::float_ aspect = py::float_(image->pixel_aspect);
    output["pixel_aspect"] = aspect;
  }
  if (!result.composites.empty()) {
    py::list composites;
    for (const auto &composite : result.composites)
      composites.append(py::bytes(reinterpret_cast<const char *>(composite.data()), Py_ssize_t(composite.size())));
    output["composites"] = composites;
  }
  if (diagnostic) {
    py::dict profile = py::dict(py::arg("prepare_seconds") = py::float_(timings.prepare_seconds),
                                py::arg("processor_seconds") = py::float_(timings.processor_seconds),
                                py::arg("transform_seconds") = py::float_(timings.transform_seconds),
                                py::arg("pack_seconds") = py::float_(timings.pack_seconds),
                                py::arg("total_native_seconds") = py::float_(timings.total_seconds),
                                py::arg("gil_reacquire_seconds") = py::float_(gil_seconds));
    output["diagnostics"] = profile;
  }
  return output;
}

py::object display_gpu_input(py::handle capsule_arg, py::handle data_arg, py::str known_key_arg,
                             py::handle copy_pixels_arg) {
  py::handle capsule, data;
  const char *known_key = "";
  int copy_pixels = 1;
  capsule = capsule_arg;
  data = data_arg;
  const auto known_key_storage = known_key_arg.cast<std::string>();
  known_key = c_string_checked(known_key_storage);
  copy_pixels = truth(copy_pixels_arg);
  const auto image = read_image(capsule);
  if (!image)
    return py::none();
  const auto settings = read_display_settings(data);
  std::shared_ptr<const ch::DisplayGpuTransform> transform;
  {
    ReleaseGIL release;
    transform = ch::display_gpu_transform(*image, settings);
  }
  if (!transform)
    return py::none();
  const auto plane =
      std::find_if(image->planes.begin(), image->planes.end(), [&](const auto &p) { return p.pass == settings.pass; });
  const auto [buffer_width, buffer_height] = ch::image_plane_size(*image, *plane);
  const auto size = Py_ssize_t(plane->pixels.size() * sizeof(float));
  py::object pixels = copy_pixels ? py::object(py::bytes(static_cast<const char *>(nullptr), size))
                                  : py::object(frame_pixels_view(image, *plane));
  if (copy_pixels) {
    auto *destination = unpublished_bytes_data(pixels);
    ReleaseGIL release;
    std::memcpy(destination, plane->pixels.data(), size);
  }
  py::dict result = py::dict(
      py::arg("generation") = py::int_(image->generation), py::arg("sequence") = py::int_(image->sequence),
      py::arg("x") = py::int_(image->x), py::arg("y") = py::int_(image->y), py::arg("width") = py::int_(image->width),
      py::arg("height") = py::int_(image->height), py::arg("full_width") = py::int_(image->full_width),
      py::arg("full_height") = py::int_(image->full_height),
      py::arg("rendered_width") = py::int_(image->rendered_width),
      py::arg("rendered_height") = py::int_(image->rendered_height), py::arg("samples") = py::int_(image->samples),
      py::arg("published_seconds") = py::float_(image->published_seconds),
      py::arg("key") = py::str(transform->key.c_str()), py::arg("channels") = py::int_(plane->channels),
      py::arg("exposure") = py::float_(settings.exposure), py::arg("gamma") = py::float_(settings.gamma),
      py::arg("opaque") = py::bool_(settings.channels == "rgb"), py::arg("pixels") = pixels);
  result["pixels_copied"] = py::bool_(copy_pixels);
  if (item(data, "compact_full_frame", false).is(py::bool_(true)) &&
      ch::can_present_compact_display(*image, buffer_width, buffer_height))
    result["compact"] = py::bool_(true);
  {
    // The same scene-linear backgrounds as the CPU route (display_image).
    std::vector<std::array<float, 3>> backgrounds;
    {
      ReleaseGIL release;
      backgrounds = ch::display_backgrounds_linear(*image, settings);
    }
    py::list values;
    for (const auto &background : backgrounds)
      values.append(py::make_tuple(background[0], background[1], background[2]));
    result["backgrounds_linear"] = values;
  }
  {
    py::int_ width = py::int_(buffer_width);
    py::int_ height = py::int_(buffer_height);
    result["buffer_width"] = width;
    result["buffer_height"] = height;
  }
  {
    py::float_ aspect = py::float_(image->pixel_aspect);
    result["pixel_aspect"] = aspect;
  }
  if (transform->key != known_key) {
    py::str shader = py::str(transform->shader.c_str());
    py::list textures = py::list(0);
    for (const auto &texture : transform->textures) {
      py::bytes values =
          py::bytes(reinterpret_cast<const char *>(texture.values.data()), texture.values.size() * sizeof(float));
      py::dict entry =
          py::dict(py::arg("sampler") = py::str(texture.sampler.c_str()), py::arg("width") = py::int_(texture.width),
                   py::arg("height") = py::int_(texture.height), py::arg("channels") = py::int_(texture.channels),
                   py::arg("linear") = py::bool_(texture.linear), py::arg("values") = values);
      textures.append(entry);
    }
    result["shader"] = shader;
    result["textures"] = textures;
  }
  return result;
}

py::object expand_display_rgba(py::handle input_arg, py::handle source_width_arg, py::handle source_height_arg,
                               py::handle width_arg, py::handle height_arg) {
  py::handle input;
  int source_width, source_height, width, height;
  input = input_arg;
  source_width = argument_integer<int>(source_width_arg);
  source_height = argument_integer<int>(source_height_arg);
  width = argument_integer<int>(width_arg);
  height = argument_integer<int>(height_arg);
  if (!py::isinstance<py::bytes>(input) &&
      (!py::isinstance<py::memoryview>(input) || py::reinterpret_borrow<py::object>(input).attr("obj").is_none()))
    throw std::invalid_argument("Display storage must be owned bytes or an owned read-only view");
  ReadBuffer buffer;
  buffer.request(input);
  if (!buffer.value.readonly)
    throw std::invalid_argument("Display storage must be read-only");
  const auto source = std::span<const std::uint8_t>(static_cast<const std::uint8_t *>(buffer.value.ptr),
                                                    (buffer.value.size * buffer.value.itemsize));
  if (source_width == width && source_height == height) {
    if (width < 1 || height < 1 || std::uint64_t(width) * height > 512ull * 1024 * 1024 ||
        source.size() != std::size_t(width) * height * 4)
      throw std::invalid_argument("Invalid full display storage");
    return py::reinterpret_borrow<py::object>(input);
  }
  std::vector<std::uint8_t> expanded;
  {
    ReleaseGIL release;
    expanded = ch::expand_display_pixels(source, source_width, source_height, width, height);
  }
  py::bytes result = py::bytes(static_cast<const char *>(nullptr), Py_ssize_t(expanded.size()));
  auto *target = unpublished_bytes_data(result);
  {
    ReleaseGIL release;
    std::memcpy(target, expanded.data(), expanded.size());
  }
  return result;
}

py::list camera_matrix_value(const ch::Matrix &matrix) {
  py::list result = py::list(12);
  for (std::size_t n = 0; n < matrix.size(); ++n) {
    auto value = py::float_(matrix[n]);
    result[n] = value;
  }
  return result;
}

py::object compact_frame_fixture(bool full_coverage) {
  // Independent development fixture; no host geometry or rendering session.
  auto frame = std::make_shared<ch::FrameEvent>(ch::harness::compact_frame_fixture());
  if (full_coverage) {
    frame->x = frame->y = 0;
    frame->width = frame->full_width = 4 * frame->rendered_width;
    frame->height = frame->full_height = 4 * frame->rendered_height;
  }
  return frame_capsule(std::move(frame));
}

py::object display_frame_fixture(py::handle pixels_arg, py::handle width_arg, py::handle height_arg) {
  // Development fixture: a combined-only frame from float32 associated RGBA
  // rows (bottom row first, as rendered), in the current working space.
  const int width = argument_integer<int>(width_arg), height = argument_integer<int>(height_arg);
  ReadBuffer buffer;
  buffer.request(pixels_arg);
  const auto bytes = std::size_t(buffer.value.size * buffer.value.itemsize);
  if (width < 1 || height < 1 || width > 16384 || height > 16384 ||
      bytes != std::size_t(width) * height * 4 * sizeof(float))
    throw std::invalid_argument("Fixture pixels must be width * height float32 RGBA values");
  auto frame = std::make_shared<ch::FrameEvent>();
  frame->generation = frame->sequence = 1;
  frame->width = frame->full_width = width;
  frame->height = frame->full_height = height;
  frame->color_configuration = ch::current_color_configuration();
  for (const auto &pass : ch::pass_catalogue())
    if (pass.id == "combined")
      frame->requested_passes.push_back(pass);
  auto &plane = frame->planes.emplace_back();
  plane.pass = "combined";
  plane.channels = 4;
  plane.pixels.resize(std::size_t(width) * height * 4);
  std::memcpy(plane.pixels.data(), buffer.value.ptr, bytes);
  return frame_capsule(std::move(frame));
}

py::object output_metadata(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto image = read_image(capsule, 0);
  py::dict result = py::dict();
  if (image && image->output_metadata)
    for (const auto &[name, value] : *image->output_metadata) {
      py::str text = py::str(value.data(), value.size());
      result[name.c_str()] = text;
    }
  return result;
}

py::object open_display(py::handle capsule_arg, py::str destination_arg, py::str socket_arg,
                        const std::map<std::string, std::string> &options) {
  py::handle capsule;
  const char *destination;
  const char *socket = "";
  capsule = capsule_arg;
  const auto destination_storage = destination_arg.cast<std::string>();
  destination = c_string_checked(destination_storage);
  const auto socket_storage = socket_arg.cast<std::string>();
  socket = c_string_checked(socket_storage);
  for (const auto &[key, value] : options) {
    if (key != "label" && key != "currentframe" && key != "framerange" && key != "rendermode" && key != "reuse_only" &&
        key != "rendersource")
      throw std::invalid_argument("Unsupported native display option: " + key);
    c_string_checked(value);
  }
  auto &h = handle(capsule);
  std::shared_ptr<const ch::SceneSnapshotState> desired;
  int width, height;
  {
    std::lock_guard lock(h.state_mutex);
    if (h.closing || h.closed)
      throw std::logic_error("Native session is closing");
    desired = h.desired;
    width = h.width;
    height = h.height;
  }
  std::vector<ch::PassDescriptor> passes;
  if (h.session)
    passes = h.session->passes();
  else
    for (const auto &pass : ch::pass_catalogue())
      if (std::find(h.config.passes.begin(), h.config.passes.end(), pass.id) != h.config.passes.end() ||
          (h.config.denoising.enabled && (pass.id == "noisy_combined" || pass.id.starts_with("denoising_"))))
        passes.push_back(pass);
  if (!h.session) {
    const auto custom = ch::aov_descriptors(desired && desired->aovs ? *desired->aovs : h.config.aovs);
    passes.insert(passes.end(), custom.begin(), custom.end());
    const auto grouped =
        ch::lightgroup_descriptors(desired && desired->lightgroups ? *desired->lightgroups : h.config.lightgroups);
    passes.insert(passes.end(), grouped.begin(), grouped.end());
    const auto crypto =
        ch::cryptomatte_descriptors(desired && desired->cryptomatte ? *desired->cryptomatte : h.config.cryptomatte);
    passes.insert(passes.end(), crypto.begin(), crypto.end());
  }
  std::shared_ptr<ch::TileDisplay> replacement;
  {
    ReleaseGIL release;
    replacement = std::make_shared<ch::TileDisplay>(destination, socket, width, height, passes, options);
  }
  std::shared_ptr<ch::TileDisplay> prior;
  bool rejected = false;
  {
    std::lock_guard lock(h.state_mutex);
    rejected = h.closing || h.closed;
    if (!rejected) {
      // Claim the destination before publishing the handle. A presenter
      // pinned to the previous display then becomes a harmless no-op.
      replacement->activate();
      prior.swap(h.display);
      h.display = std::move(replacement);
    }
  }
  {
    ReleaseGIL release;
    prior.reset();
    replacement.reset();
  }
  if (rejected)
    throw std::logic_error("Native session is closing");
  return py::none();
}

std::shared_ptr<ch::TileDisplay> pin_display(Handle &h) {
  std::lock_guard lock(h.state_mutex);
  if (h.closing || h.closed || !h.display)
    throw std::logic_error("No native display attached");
  return h.display;
}

void present_pinned(std::shared_ptr<ch::TileDisplay> display, FramePtr image, std::uint64_t sequence) {
  try {
    if (!image)
      throw std::logic_error("No native frame is available for presentation");
    ReleaseGIL release;
    display->present(std::move(image), sequence);
    display.reset(); // Socket retirement never joins on this caller.
  } catch (...) {
    ReleaseGIL release;
    display.reset();
    throw;
  }
}

py::object present(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  if (!h.session)
    throw std::logic_error("A rendering session is required for native presentation");
  auto display = pin_display(h);
  try {
    auto frame = h.session->frame();
    if (frame)
      present_pinned(std::move(display), std::move(frame), 0);
  } catch (...) {
    ReleaseGIL release;
    display.reset();
    throw;
  }
  {
    ReleaseGIL release;
    display.reset();
  }
  return py::none();
}

py::object close_display(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::shared_ptr<ch::TileDisplay> prior;
  {
    std::lock_guard lock(h.state_mutex);
    prior.swap(h.display);
  }
  {
    ReleaseGIL release;
    prior.reset();
  }
  return py::none();
}

py::object owned_frame(py::handle capsule_arg, py::handle after_arg) {
  py::handle capsule;
  unsigned long long after = 0;
  capsule = capsule_arg;
  after = unsigned_argument_mask(after_arg);
  return frame_capsule(handle(capsule).session->frame(after));
}

py::object present_frame(py::handle capsule_arg, py::handle image_arg, py::handle sequence_arg) {
  py::handle capsule, image;
  unsigned long long sequence = 0;
  capsule = capsule_arg;
  image = image_arg;
  sequence = unsigned_argument_mask(sequence_arg, 3);
  auto &h = handle(capsule);
  auto display = pin_display(h);
  try {
    auto owned = read_image(image);
    present_pinned(std::move(display), std::move(owned), sequence);
  } catch (...) {
    ReleaseGIL release;
    display.reset();
    throw;
  }
  return py::none();
}

py::object test_wait_display(py::handle capsule_arg) {
  auto display = pin_display(handle(capsule_arg));
  std::uint64_t completed_writes{};
  try {
    ReleaseGIL release;
    completed_writes = display->wait_for_idle();
    display.reset();
  } catch (...) {
    ReleaseGIL release;
    display.reset();
    throw;
  }
  return py::int_(completed_writes);
}

bool display_ready(py::handle capsule_arg) {
  auto display = pin_display(handle(capsule_arg));
  ReleaseGIL release;
  return display->ready();
}

bool set_native_display_status(py::handle capsule_arg, std::string message) {
  auto &h = handle(capsule_arg);
  ReleaseGIL release;
  std::shared_ptr<ch::TileDisplay> display;
  {
    std::lock_guard lock(h.state_mutex);
    if (h.closing || h.closed || !h.display)
      return false;
    display = h.display;
  }
  return display->set_failure_status(std::move(message));
}

// Test-only rendezvous after admission. Python Events provide a bounded wait
// without introducing timing sleeps or callbacks into the production path.
py::object test_present_frame_rendezvous(py::handle capsule_arg, py::handle image_arg, py::handle entered_arg,
                                         py::handle release_arg) {
  auto &h = handle(capsule_arg);
  auto display = pin_display(h);
  try {
    auto image = read_image(image_arg);
    entered_arg.attr("set")();
    if (!py::cast<bool>(release_arg.attr("wait")(10.0)))
      throw std::runtime_error("Timed out waiting to release native presenter");
    present_pinned(std::move(display), std::move(image), 0);
  } catch (...) {
    ReleaseGIL release;
    display.reset();
    throw;
  }
  return py::none();
}

py::object set_preview_pass(py::handle capsule_arg, py::str pass_arg, py::handle focus_arg) {
  py::handle capsule;
  const char *pass;
  int focus = 0;
  capsule = capsule_arg;
  const auto pass_storage = pass_arg.cast<std::string>();
  pass = c_string_checked(pass_storage);
  focus = truth(focus_arg);
  auto &h = handle(capsule);
  if (!h.session)
    throw std::logic_error("Preview preference belongs to the rendering backend");
  h.session->set_preview({pass, focus != 0});
  return py::none();
}

py::object focus_frame(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  if (!h.session)
    throw std::logic_error("Focus buffers belong to the rendering backend");
  return frame_capsule(h.session->focus_frame());
}

py::object sample_focus(py::handle capsule_arg, py::handle x_arg, py::handle y_arg) {
  try {
    py::handle capsule;
    int x, y;
    capsule = capsule_arg;
    x = argument_integer<int>(x_arg);
    y = argument_integer<int>(y_arg);
    const auto frame = read_image(capsule);
    if (!frame)
      return py::none();
    const auto value = ch::sample_focus(*frame, x, y);
    return py::dict(
        py::arg("generation") = py::int_(frame->generation), py::arg("sequence") = py::int_(frame->sequence),
        py::arg("position") =
            py::make_tuple(py::float_(value.position.x), py::float_(value.position.y), py::float_(value.position.z)),
        py::arg("depth") = py::float_(value.depth), py::arg("rendered_width") = py::int_(frame->rendered_width),
        py::arg("rendered_height") = py::int_(frame->rendered_height));
  } catch (const std::invalid_argument &e) {
    throw py::value_error(e.what());
  }
}

py::object pass_catalogue() {
  py::list result = py::list(0);
  for (const auto &pass : ch::pass_catalogue()) {
    py::object value = pass_value(pass);
    result.append(value);
  }
  return result;
}
} // namespace cycles_houdini::python_detail
