// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/session.h"
#include "color_internal.h"
#include "exr_validation.h"
#include "pass_catalogue.h"
#include "staged_output.h"
#include <OpenEXR/openexr.h>
#include <OpenImageIO/filesystem.h>
#include <OpenImageIO/imageio.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <limits>
#include <set>
#include <stdexcept>
namespace cycles_houdini {
namespace {
[[noreturn]] void unreadable_exr(const std::string &filename, const std::string &detail) {
  throw std::runtime_error("Existing EXR pixel data is unreadable. Use Overwrite to replace it: " + filename + ": " +
                           detail);
}

void check_exr_chunks(OIIO::Filesystem::IOFile &io, const std::string &filename,
                      const std::vector<OIIO::ImageSpec> &specs, const std::function<void()> &checkpoint) {
  // OIIO's SDK reader can return pixels for a short uncompressed chunk.
  // Check all chunk extents against the same open file before decoding.
  struct Context {
    exr_context_t value = nullptr;
    ~Context() {
      if (value)
        exr_finish(&value);
    }
  } context;
  const auto check = [&](exr_result_t result) {
    if (result != EXR_ERR_SUCCESS)
      unreadable_exr(filename, exr_get_error_code_as_string(result));
  };
  const auto size = io.size();
  if (size > std::size_t(std::numeric_limits<int64_t>::max()))
    unreadable_exr(filename, "EXR file exceeds the supported offset range");
  exr_context_initializer_t options = EXR_DEFAULT_CONTEXT_INITIALIZER;
  options.flags = EXR_CONTEXT_FLAG_DISABLE_CHUNK_RECONSTRUCTION;
  options.user_data = &io;
  options.size_fn = [](exr_const_context_t, void *data) {
    return int64_t(static_cast<OIIO::Filesystem::IOFile *>(data)->size());
  };
  options.read_fn = [](exr_const_context_t, void *data, void *buffer, uint64_t count, uint64_t offset,
                       exr_stream_error_func_ptr_t) -> int64_t {
    if (count > std::numeric_limits<std::size_t>::max() || offset > uint64_t(std::numeric_limits<int64_t>::max()))
      return -1;
    return int64_t(static_cast<OIIO::Filesystem::IOFile *>(data)->pread(buffer, std::size_t(count), int64_t(offset)));
  };
  check(exr_start_read(&context.value, filename.c_str(), &options));
  int parts = 0;
  check(exr_get_count(context.value, &parts));
  if (std::size_t(parts) != specs.size())
    unreadable_exr(filename, "EXR part count changed during validation");
  std::size_t checked = 0;
  const auto check_extent = [&](const exr_chunk_info_t &chunk) {
    if (chunk.data_offset > size || chunk.packed_size > size - chunk.data_offset)
      unreadable_exr(filename, "EXR chunk extends past the end of the file");
    if (checkpoint && ++checked % 32 == 0)
      checkpoint();
  };
  for (int part = 0; part < parts; ++part) {
    if (checkpoint)
      checkpoint();
    const auto &spec = specs[part];
    check(exr_validate_chunk_table(context.value, part));
    exr_chunk_info_t chunk{};
    if (spec.tile_width) {
      const int nx = 1 + (spec.width - 1) / spec.tile_width, ny = 1 + (spec.height - 1) / spec.tile_height;
      for (int y = 0; y < ny; ++y)
        for (int x = 0; x < nx; ++x) {
          check(exr_read_tile_chunk_info(context.value, part, x, y, 0, 0, &chunk));
          check_extent(chunk);
        }
    } else {
      int32_t lines = 0;
      check(exr_get_scanlines_per_chunk(context.value, part, &lines));
      if (lines < 1)
        unreadable_exr(filename, "Invalid EXR scanline block geometry");
      for (int64_t y = spec.y; y < int64_t(spec.y) + spec.height; y += lines) {
        check(exr_read_scanline_chunk_info(context.value, part, int(y), &chunk));
        check_extent(chunk);
      }
    }
  }
}

void check_exr_pixels(OIIO::ImageInput &input, int part, const OIIO::ImageSpec &spec, const std::string &filename,
                      const std::function<void()> &checkpoint) {
  const auto before_read = [&] {
    if (checkpoint)
      checkpoint();
  };
  const auto check = [&](bool readable) {
    if (!readable)
      unreadable_exr(filename, input.geterror());
  };
  // Traverse all channels in an 8 MiB reusable byte buffer when possible.
  // The reader needs at least one complete scanline or tile of one channel.
  constexpr std::size_t budget = 8u * 1024u * 1024u;
  const auto channel_batch = [&](std::size_t pixels) {
    return int(std::clamp<std::size_t>(budget / pixels, 1, spec.nchannels));
  };
  const int xend = spec.x + spec.width, yend = spec.y + spec.height;
  if (spec.tile_width) {
    const auto tile_pixels =
        std::size_t(std::min(spec.tile_width, spec.width)) * std::min(spec.tile_height, spec.height);
    const int channels = channel_batch(tile_pixels);
    std::vector<std::uint8_t> pixels(tile_pixels * channels);
    for (int y = spec.y; y < yend;) {
      const int ymax = y + std::min(spec.tile_height, yend - y);
      for (int x = spec.x; x < xend;) {
        const int xmax = x + std::min(spec.tile_width, xend - x);
        for (int first = 0; first < spec.nchannels;) {
          const int last = first + std::min(channels, spec.nchannels - first);
          before_read();
          check(input.read_tiles(part, 0, x, xmax, y, ymax, spec.z, spec.z + 1, first, last, OIIO::TypeDesc::UINT8,
                                 pixels.data()));
          first = last;
        }
        x = xmax;
      }
      y = ymax;
    }
    return;
  }
  const int channels = channel_batch(spec.width);
  const auto row_bytes = std::size_t(spec.width) * channels;
  const int rows = int(std::clamp<std::size_t>(budget / row_bytes, 1, spec.height));
  const int band = rows < 32 ? rows : rows - rows % 32;
  std::vector<std::uint8_t> pixels(row_bytes * band);
  for (int y = spec.y; y < yend;) {
    const int ymax = y + std::min(band, yend - y);
    for (int first = 0; first < spec.nchannels;) {
      const int last = first + std::min(channels, spec.nchannels - first);
      before_read();
      check(input.read_scanlines(part, 0, y, ymax, spec.z, first, last, OIIO::TypeDesc::UINT8, pixels.data()));
      first = last;
    }
    y = ymax;
  }
}

void check_exr_payload(OIIO::Filesystem::IOFile &file, OIIO::ImageInput &input,
                       const std::vector<OIIO::ImageSpec> &specs, const std::string &filename,
                       const std::function<void()> &checkpoint = {}) {
  for (const auto &spec : specs)
    if (spec.deep || spec.depth != 1 || spec.z != 0 || spec.nchannels < 1 || spec.width < 1 || spec.height < 1 ||
        uint64_t(spec.width) * spec.height > 512ull * 1024 * 1024 ||
        int64_t(spec.x) + spec.width > std::numeric_limits<int>::max() ||
        int64_t(spec.y) + spec.height > std::numeric_limits<int>::max() ||
        (spec.tile_width && (spec.tile_width < 1 || spec.tile_height < 1)))
      unreadable_exr(filename, "Invalid flat EXR geometry");
  check_exr_chunks(file, filename, specs, checkpoint);
  for (std::size_t index = 0; index < specs.size(); ++index)
    check_exr_pixels(input, int(index), specs[index], filename, checkpoint);
}

// The Output Picture's extension selects the format, as in Mantra and Karma. Only
// these extensions change it; every other name keeps writing EXR exactly as before.
std::string image_format(const std::string &filename) {
  std::string extension;
  for (const auto c : std::filesystem::u8path(filename).extension().u8string())
    extension += char(std::tolower(static_cast<unsigned char>(c)));
  if (extension == ".png")
    return "png";
  if (extension == ".jpg" || extension == ".jpeg")
    return "jpeg";
  if (extension == ".tif" || extension == ".tiff")
    return "tiff";
  return "openexr";
}
std::string format_label(const std::string &format) {
  return format == "png" ? "PNG" : format == "jpeg" ? "JPEG" : format == "tiff" ? "TIFF" : "EXR";
}
// Caller entries are applied before the writer's own, so colour, provenance,
// compression and Cryptomatte entries always win. Structural names are refused.
void add_metadata(OIIO::ImageSpec &spec, const std::vector<RenderSession::OutputAttribute> &metadata) {
  static const std::set<std::string> reserved = {"channels",      "chunkCount", "compression", "dataWindow",
                                                 "displayWindow", "lineOrder",  "name",        "tiles",
                                                 "type",          "version"};
  for (const auto &a : metadata) {
    if (a.name.empty() || a.name.size() > 255 || a.name.find('\0') != std::string::npos || reserved.contains(a.name) ||
        a.name.starts_with("oiio:") || a.name.starts_with("openexr:") || a.name.starts_with("cryptomatte/"))
      throw std::invalid_argument("Invalid or reserved output header entry: " + a.name);
    const std::size_t count = a.type == "string"                                               ? 0
                              : a.type == "rational" || a.type == "int2" || a.type == "float2" ? 2
                                                                                               : 1;
    const bool integer = a.type == "int" || a.type == "rational" || a.type == "int2";
    if (a.numbers.size() != count || std::any_of(a.numbers.begin(), a.numbers.end(), [&](double v) {
          return !std::isfinite(v) || (integer && (v != std::trunc(v) || std::abs(v) > 2147483647.0));
        }))
      throw std::invalid_argument("Output header entry has an invalid value: " + a.name);
    if (a.type == "string")
      spec.attribute(a.name, a.text);
    else if (a.type == "int")
      spec.attribute(a.name, int(a.numbers[0]));
    else if (a.type == "float")
      spec.attribute(a.name, float(a.numbers[0]));
    else if (a.type == "double")
      spec.attribute(a.name, OIIO::TypeDesc::DOUBLE, &a.numbers[0]);
    else if (a.type == "rational") {
      if (a.numbers[1] <= 0)
        throw std::invalid_argument("Output header rational needs a positive denominator: " + a.name);
      const int value[2]{int(a.numbers[0]), int(a.numbers[1])};
      spec.attribute(a.name, OIIO::TypeRational, value);
    } else if (a.type == "int2") {
      const int value[2]{int(a.numbers[0]), int(a.numbers[1])};
      spec.attribute(a.name, OIIO::TypeDesc(OIIO::TypeDesc::INT, OIIO::TypeDesc::VEC2), value);
    } else if (a.type == "float2") {
      const float value[2]{float(a.numbers[0]), float(a.numbers[1])};
      spec.attribute(a.name, OIIO::TypeDesc(OIIO::TypeDesc::FLOAT, OIIO::TypeDesc::VEC2), value);
    } else
      throw std::invalid_argument("Unknown output header entry type: " + a.type);
  }
}
// What the renderer itself knows about this frame. No host, user or path.
void add_render_metadata(OIIO::ImageSpec &spec, const FrameEvent &image, const SessionStatus &s) {
  // Karma and Mantra name the wall-clock render time renderTime, in seconds.
  const double seconds = image.published_seconds;
  spec.attribute("renderTime", OIIO::TypeDesc::DOUBLE, &seconds);
  spec.attribute("cycles_houdini:samples_requested", s.rendering.samples);
  spec.attribute("cycles_houdini:samples_rendered", image.samples);
  std::string devices;
  for (const auto &id : s.render_device_ids)
    devices += (devices.empty() ? "" : ",") + id;
  spec.attribute("cycles_houdini:render_devices", devices);
  const int resolution[2]{image.full_width, image.full_height};
  spec.attribute("cycles_houdini:resolution", OIIO::TypeDesc(OIIO::TypeDesc::INT, OIIO::TypeDesc::VEC2), resolution);
  // OpenEXR capDate is local time; UTC = local time + utcOffset (seconds).
  const auto now = std::time(nullptr);
  std::tm local{}, utc{};
#ifdef _WIN32
  localtime_s(&local, &now);
  gmtime_s(&utc, &now);
#else
  localtime_r(&now, &local);
  gmtime_r(&now, &utc);
#endif
  char text[32]{};
  std::strftime(text, sizeof(text), "%Y:%m:%d %H:%M:%S", &local);
  spec.attribute("DateTime", text);
  utc.tm_isdst = local.tm_isdst;
  spec.attribute("utcOffset", float(std::difftime(std::mktime(&utc), now)));
}
// PNG, JPEG and TIFF hold the beauty image only. PNG and JPEG are display-referred
// through the same display_image() conversion the viewer shows and saves; TIFF is
// scene-linear float, like Mantra and Karma write it.
void write_image(const std::string &filename, const std::string &format, const RenderSession::OutputOptions &options,
                 const FrameEvent &image, OIIO::ImageSpec spec,
                 const OCIO_NAMESPACE::ConstCPUProcessorRcPtr &processor) {
  const auto label = format_label(format);
  if (options.multipart)
    throw std::invalid_argument("Multipart output requires an .exr filename: " + filename);
  const auto internal = [&](const std::string &pass) {
    const auto found = std::find_if(image.requested_passes.begin(), image.requested_passes.end(),
                                    [&](const auto &d) { return d.id == pass; });
    return found != image.requested_passes.end() && found->internal;
  };
  const bool beauty_only = options.passes.empty()
                               ? std::none_of(image.planes.begin(), image.planes.end(),
                                              [&](const auto &p) { return p.pass != "combined" && !internal(p.pass); })
                               : options.passes.size() == 1 && options.passes.front().source == "combined" &&
                                     options.passes.front().layer.empty();
  if (!beauty_only)
    throw std::invalid_argument(label + " output holds only the beauty image; write passes to EXR files: " + filename);
  const auto plane =
      std::find_if(image.planes.begin(), image.planes.end(), [](const auto &p) { return p.pass == "combined"; });
  if (plane == image.planes.end() || (plane->channels != 3 && plane->channels != 4))
    throw std::runtime_error("Output frame has no beauty image");
  const bool display = format != "tiff";
  const bool alpha = options.alpha && format != "jpeg";
  const int channels = alpha ? 4 : 3;
  const bool crop = options.crop_to_region;
  const int width = crop ? image.width : image.full_width, height = crop ? image.height : image.full_height;
  const int left = crop ? 0 : image.x, top = crop ? 0 : image.full_height - image.y - image.height;
  const auto region = std::size_t(image.width) * image.height;
  // Region pixels, top-down, four channels. Outside a render region stays transparent black.
  std::vector<std::uint8_t> bytes;
  std::vector<float> floats;
  // JPEG has no alpha: the image over black, View(RGB_premult), composited in
  // scene-linear light before the view (associated RGB, no division by alpha).
  bool over_black = false;
  if (display && options.color_space.empty()) {
    // The viewer's default picture: the active OCIO default display and view,
    // no exposure or gamma change. Straight (unassociated) alpha, as PNG requires.
    DisplaySettings settings;
    if (format == "jpeg")
      settings.backgrounds = {{0, 0, 0}};
    auto converted = display_image(image, settings);
    over_black = !converted.composites.empty();
    bytes = over_black ? std::move(converted.composites.front()) : std::move(converted.rgba);
    if (bytes.size() != region * 4)
      throw std::logic_error("Display image does not match the output frame");
    const auto target = presentation_color_configuration();
    const auto &policy = target->policy();
    std::string space;
    if (!policy.views.empty()) {
      const auto &display_name = policy.default_display;
      const auto &view = policy.default_views.at(display_name);
      const char *name =
          owned_color_configuration(*target)->getDisplayViewColorSpaceName(display_name.c_str(), view.c_str());
      space = name ? name : "";
      spec.attribute("cycles_houdini:display", display_name);
      spec.attribute("cycles_houdini:view", view);
    }
    spec.attribute("oiio:ColorSpace", space);
  } else {
    auto source = expand_image_plane(image, *plane);
    if (source.size() != region * plane->channels)
      throw std::logic_error("Beauty plane does not match the output frame");
    std::vector<float> rgba(region * 4);
    for (int y = 0; y < image.height; ++y)
      for (int x = 0; x < image.width; ++x) {
        const auto from = (std::size_t(image.height - 1 - y) * image.width + x) * plane->channels;
        auto *to = rgba.data() + (std::size_t(y) * image.width + x) * 4;
        for (int c = 0; c < 3; ++c)
          to[c] = source[from + c];
        to[3] = plane->channels == 4 ? source[from + 3] : 1.f;
      }
    over_black = format == "jpeg";
    if ((display || (processor && !processor->isNoOp())) && !over_black)
      for (std::size_t i = 0; i < region; ++i)
        if (rgba[i * 4 + 3] != 0)
          for (int c = 0; c < 3; ++c)
            rgba[i * 4 + c] /= rgba[i * 4 + 3];
    if (processor && !processor->isNoOp()) {
      OCIO_NAMESPACE::PackedImageDesc pixels(rgba.data(), image.width, image.height, 3, OCIO_NAMESPACE::BIT_DEPTH_F32,
                                             sizeof(float), 4 * sizeof(float),
                                             std::ptrdiff_t(image.width) * 4 * sizeof(float));
      processor->apply(pixels);
    }
    if (display) {
      // Output Color Override: the chosen OCIO colour space, encoded as 8 bits.
      bytes.resize(region * 4);
      for (std::size_t i = 0; i < region * 4; ++i)
        bytes[i] = std::uint8_t(std::lround(std::clamp(std::isfinite(rgba[i]) ? rgba[i] : 0.f, 0.f, 1.f) * 255));
    } else {
      if (processor && !processor->isNoOp())
        for (std::size_t i = 0; i < region; ++i)
          if (rgba[i * 4 + 3] != 0)
            for (int c = 0; c < 3; ++c)
              rgba[i * 4 + c] *= rgba[i * 4 + 3];
      floats = std::move(rgba);
    }
  }
  std::vector<std::uint8_t> out_bytes;
  std::vector<float> out_floats;
  if (display)
    out_bytes.assign(std::size_t(width) * height * channels, 0);
  else
    out_floats.assign(std::size_t(width) * height * channels, 0.f);
  for (int y = 0; y < image.height; ++y)
    for (int x = 0; x < image.width; ++x) {
      const auto from = (std::size_t(y) * image.width + x) * 4;
      const auto to = (std::size_t(top + y) * width + left + x) * channels;
      if (!display) {
        for (int c = 0; c < channels; ++c)
          out_floats[to + c] = floats[from + c];
        continue;
      }
      const unsigned a = bytes[from + 3];
      for (int c = 0; c < 3; ++c) {
        unsigned v = bytes[from + c];
        if (format == "jpeg" && !over_black) {
          // An image without composites (no alpha channel, or a frame from another
          // rendering policy): the display-space premultiply of the straight bytes.
          const unsigned t = v * a;
          v = (t + (t >> 8) + 0x80) >> 8;
        }
        out_bytes[to + c] = std::uint8_t(v);
      }
      if (alpha)
        out_bytes[to + 3] = std::uint8_t(a);
    }
  spec.x = spec.y = spec.full_x = spec.full_y = 0;
  spec.width = spec.full_width = width;
  spec.height = spec.full_height = height;
  spec.nchannels = channels;
  spec.channelnames = {"R", "G", "B"};
  if (alpha)
    spec.channelnames.push_back("A");
  spec.channelformats.clear();
  spec.set_format(display ? OIIO::TypeDesc::UINT8 : OIIO::TypeDesc::FLOAT);
  spec.alpha_channel = alpha ? 3 : -1;
  if (display) {
    // A display-referred file has no scene-linear interop identity.
    spec.erase_attribute("colorInteropID");
    spec.erase_attribute("cycles_houdini:output_color_identity");
    spec.attribute("cycles_houdini:alpha_convention", alpha ? "unassociated" : "none");
    // The bytes are already straight; without this OIIO would divide them by alpha again.
    spec.attribute("oiio:UnassociatedAlpha", 1);
    if (format == "jpeg")
      spec.attribute("CompressionQuality", 100); // Mantra's vm_image_jpeg_quality default.
  } else {
    spec.attribute("compression", "lzw"); // Mantra's vm_image_tiff_compression default.
    if (!alpha)
      spec.attribute("cycles_houdini:alpha_convention", "none");
  }
  const auto validate_existing = [&] {
    const auto path = std::filesystem::u8path(filename);
    if (!std::filesystem::is_regular_file(std::filesystem::symlink_status(path)))
      throw std::runtime_error("Existing output is not a regular file: " + filename);
    auto input = OIIO::ImageInput::open(filename);
    if (!input) {
      const auto error = OIIO::geterror();
      throw std::runtime_error("Existing output is not a readable " + label + ": " + filename + ": " + error);
    }
    if (std::string(input->format_name()) != format)
      throw std::runtime_error("Existing output is not a " + label + " file: " + filename);
    const auto &actual = input->spec();
    if (actual.x != 0 || actual.y != 0 || actual.width != width || actual.height != height ||
        actual.full_width != width || actual.full_height != height || actual.nchannels != channels)
      throw std::runtime_error("Existing " + label + " dimensions or channels do not match: " + filename);
    if (options.fail_on_missing_inputs) {
      if (actual.get_int_attribute("cycles_houdini:input_validation_version", 0) != 1 ||
          actual.get_int_attribute("cycles_houdini:missing_input_count", -1) < 0 ||
          actual.get_string_attribute("cycles_houdini:source_diagnostics").empty())
        throw std::runtime_error("Existing " + label +
                                 " has uncertified render-input provenance; use Overwrite to replace it: " + filename);
      if (actual.get_int_attribute("cycles_houdini:missing_input_count", -1) != 0)
        throw std::runtime_error("Existing " + label +
                                 " contains missing render inputs; use Overwrite after repairing inputs: " + filename);
    }
    input->close();
  };
  if (options.existing_files != "overwrite" &&
      std::filesystem::exists(std::filesystem::symlink_status(std::filesystem::u8path(filename)))) {
    if (options.existing_files == "error")
      throw std::runtime_error("Render output already exists: " + filename);
    validate_existing();
    return;
  }
  StagedOutput staged(filename, format == "png" ? ".png" : format == "jpeg" ? ".jpg" : ".tif");
  const auto temporary = staged.temporary_filename();
  auto output = OIIO::ImageOutput::create(temporary);
  if (!output)
    throw std::runtime_error("Could not create output: " + filename + ": " + OIIO::geterror());
  if (!output->open(temporary, spec))
    throw std::runtime_error(output->geterror());
  const bool written = display ? output->write_image(OIIO::TypeDesc::UINT8, out_bytes.data())
                               : output->write_image(OIIO::TypeDesc::FLOAT, out_floats.data());
  if (!written)
    throw std::runtime_error(label + " write failed: " + output->geterror());
  if (!output->close())
    throw std::runtime_error(label + " close failed: " + output->geterror());
  if (!staged.commit(options.existing_files == "overwrite")) {
    if (options.existing_files == "error")
      throw std::runtime_error("Render output already exists: " + filename);
    validate_existing();
  }
}
} // namespace
void inspect_existing_exr_file(const std::string &filename, int expected_parts,
                               const std::function<bool(const std::vector<OIIO::ImageSpec> &)> &validate,
                               const std::function<void()> &checkpoint) {
  if (expected_parts < 1)
    throw std::invalid_argument("Expected EXR part count must be positive");
  OIIO::Filesystem::IOFile file(filename, OIIO::Filesystem::IOProxy::Read);
  OIIO::ImageSpec hints;
  hints.attribute("oiio:missingcolor", "");
  auto input = OIIO::ImageInput::open(filename, &hints, &file);
  if (!input)
    throw std::runtime_error("Existing output is not a readable EXR: " + filename + ": " + OIIO::geterror());
  if (std::string_view(input->format_name()) != "openexr")
    throw std::runtime_error("Existing output is not an EXR: " + filename);
  std::vector<OIIO::ImageSpec> specs;
  for (int part = 0; part <= expected_parts; ++part) {
    if (checkpoint)
      checkpoint();
    OIIO::ImageSpec spec;
    if (!input->seek_subimage(part, 0, spec)) {
      input->geterror();
      break;
    }
    specs.push_back(std::move(spec));
  }
  if (validate(specs))
    check_exr_payload(file, *input, specs, filename, checkpoint);
}

void RenderSession::write_exr(const std::string &filename) const { write_exr(filename, OutputOptions{}); }
void RenderSession::write_exr(const std::string &filename, const OutputOptions &options) const {
  if (options.existing_files != "overwrite" && options.existing_files != "skip" && options.existing_files != "error")
    throw std::invalid_argument("Unknown existing files policy: " + options.existing_files);
  if (!std::isfinite(options.pixel_aspect) || options.pixel_aspect < 0)
    throw std::invalid_argument("Pixel aspect must be finite and positive");
  const std::set<std::string> codecs = {"none", "rle", "zip", "zips", "piz", "pxr24", "b44", "b44a", "dwaa", "dwab"};
  const auto validate_codec = [&](const std::string &codec, float level) {
    if (!codecs.contains(codec))
      throw std::invalid_argument("Unsupported EXR compression: " + codec);
    if (!std::isfinite(level) || level < 0)
      throw std::invalid_argument("DWA compression level must be finite and nonnegative");
  };
  validate_codec(options.compression, options.dwa_level);
  auto image = frame();
  const auto s = status();
  if (!image || !image->complete || image->generation != s.generation)
    throw std::runtime_error("No complete image for the current generation");
  if (s.state == SessionState::Failed || s.state == SessionState::Cancelled)
    throw std::runtime_error("Cannot write a failed or cancelled render");
  if (options.fail_on_missing_inputs)
    for (const auto &issue : s.source_diagnostics)
      if (missing_render_input(issue))
        throw std::runtime_error("Missing render input [" + issue.code + "] " + issue.source + ": " + issue.message);
  if (!image->color_configuration)
    throw std::logic_error("Output frame has no source color configuration");
  const auto &catalogue = image->requested_passes;
  const auto descriptor = [&](const std::string &id) -> const PassDescriptor & {
    const auto found = std::find_if(catalogue.begin(), catalogue.end(), [&](const auto &d) { return d.id == id; });
    if (found == catalogue.end())
      throw std::logic_error("Missing output pass descriptor: " + id);
    return *found;
  };
  const auto &color = image->color_configuration->policy();
  std::string output_space = color.working_space;
  ColorInteropIdentity output_identity{color.output_interop_id, color.output_interop_selection};
  OCIO_NAMESPACE::ConstCPUProcessorRcPtr processor;
  if (!options.color_space.empty()) {
    if (!options.color_configuration.empty() && options.color_configuration != color.configuration_id)
      throw std::invalid_argument(
          "Output OCIO configuration changed since this frame. Render again before saving with an override.");
    const auto config = owned_color_configuration(*image->color_configuration);
    const auto target = config->getColorSpace(options.color_space.c_str());
    if (!target || target->isData())
      throw std::invalid_argument("Output requires a valid non-data OCIO color space: " + options.color_space);
    output_space = target->getName();
    processor = config->getProcessor(color.working_space.c_str(), output_space.c_str())->getDefaultCPUProcessor();
    auto tagged = config->createEditableCopy();
    tagged->setRole(OCIO_NAMESPACE::ROLE_SCENE_LINEAR, output_space.c_str());
    output_identity = working_interop_identity(tagged);
  }
  OIIO::ImageSpec common(image->width, image->height, 1, OIIO::TypeDesc::FLOAT);
  common.x = image->x;
  common.y = image->full_height - image->y - image->height;
  common.full_x = common.full_y = 0;
  common.full_width = image->full_width;
  common.full_height = image->full_height;
  if (options.crop_to_region) {
    common.x = common.y = 0;
    common.full_width = common.width;
    common.full_height = common.height;
  }
  add_metadata(common, options.metadata);
  common.attribute("cycles_houdini:render_working_space", color.working_space);
  common.attribute("oiio:ColorSpace", output_space);
  common.attribute("cycles:engine_commit", capabilities().engine_commit);
  common.attribute("colorInteropID", output_identity.id);
  common.attribute("cycles_houdini:output_color_identity", output_identity.selection);
  common.attribute("cycles_houdini:ocio_builtin_configuration", color.builtin_configuration);
  common.attribute("cycles_houdini:ocio_configuration", color.configuration_id);
  common.attribute("cycles_houdini:render_color_policy", color.render_policy_id);
  common.attribute("cycles_houdini:generation", static_cast<int>(image->generation));
  common.attribute("cycles_houdini:volume_integrator", s.volume_ray_marching ? "ray_marching" : "octree");
  common.attribute("cycles_houdini:alpha_convention", "associated");
  common.attribute("cycles_houdini:denoiser", image->denoiser);
  common.attribute("cycles_houdini:denoise_device", image->denoise_device_id);
  common.attribute("PixelAspectRatio", options.pixel_aspect > 0 ? options.pixel_aspect : image->pixel_aspect);
  if (image->output_metadata)
    for (const auto &[name, value] : *image->output_metadata)
      if (!name.starts_with("cryptomatte/"))
        common.attribute(name, value);
  // A strict resume must certify the pixels already on disk as well as the
  // current scene. Older/external EXRs without this contract fail closed.
  common.attribute("cycles_houdini:input_validation_version", 1);
  common.attribute(
      "cycles_houdini:missing_input_count",
      static_cast<int>(std::count_if(s.source_diagnostics.begin(), s.source_diagnostics.end(), missing_render_input)));
  {
    std::string report = "[";
    for (const auto &d : s.source_diagnostics) {
      if (report.size() > 1)
        report += ",";
      report += "{\"code\":" + json_string(d.code) + ",\"source\":" + json_string(d.source) +
                ",\"resource\":" + json_string(d.resource) + ",\"severity\":" + json_string(d.severity) +
                ",\"message\":" + json_string(d.message) + ",\"action\":" + json_string(d.action) +
                ",\"attribute\":" + json_string(d.attribute) + "}";
    }
    common.attribute("cycles_houdini:source_diagnostics", report + "]");
  }
  add_render_metadata(common, *image, s);
  if (const auto format = image_format(filename); format != "openexr") {
    write_image(filename, format, options, *image, common, processor);
    return;
  }
  struct Part {
    std::string name, compression;
    float dwa_level{};
    std::vector<const ImagePlane *> planes;
  };
  std::vector<Part> parts;
  std::vector<OIIO::ImageSpec> specs;
  auto selected = options.passes;
  if (selected.empty())
    for (const auto &p : image->planes)
      if (!descriptor(p.pass).internal)
        selected.push_back({p.pass, p.pass == "combined" ? "" : p.pass});
  const auto identifier = [](const std::string &name) {
    const auto alpha = [](char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_'; };
    return !name.empty() && alpha(name.front()) &&
           std::all_of(name.begin(), name.end(), [&](char c) { return alpha(c) || (c >= '0' && c <= '9'); });
  };
  for (std::size_t i = 0; i < selected.size(); ++i) {
    const auto &selection = selected[i];
    if (selection.precision != "inherit" && selection.precision != "half" && selection.precision != "float")
      throw std::invalid_argument("Unknown output pass precision: " + selection.precision);
    const auto found = std::find_if(image->planes.begin(), image->planes.end(),
                                    [&](const auto &p) { return p.pass == selection.source; });
    if (found == image->planes.end())
      throw std::invalid_argument("Output pass is unavailable: " + selection.source);
    const auto &d = descriptor(found->pass);
    if (d.internal)
      throw std::invalid_argument("Internal denoising pass is not selected for output: " + found->pass);
    const auto layer = selection.layer.empty() && found->pass != "combined" ? found->pass : selection.layer;
    if (!layer.empty() && !identifier(layer))
      throw std::invalid_argument("Invalid output layer name: " + layer);
    if (d.cryptomatte && (layer.size() < 3 || found->pass.size() < 3 ||
                          layer.substr(layer.size() - 2) != found->pass.substr(found->pass.size() - 2)))
      throw std::invalid_argument("Cryptomatte output must retain its numbered layer suffix");
    const auto part = options.multipart ? (selection.part.empty() ? (d.cryptomatte   ? layer.substr(0, layer.size() - 2)
                                                                     : layer.empty() ? "Beauty"
                                                                                     : layer)
                                                                  : selection.part)
                                        : "";
    if (options.multipart && !identifier(part))
      throw std::invalid_argument("Invalid multipart EXR part name: " + part);
    const auto codec =
        options.multipart && !selection.compression.empty() ? selection.compression : options.compression;
    const float level =
        codec == "dwaa" || codec == "dwab"
            ? (options.multipart && !selection.compression.empty() ? selection.dwa_level : options.dwa_level)
            : 45.f;
    validate_codec(codec, level);
    auto index =
        std::find_if(parts.begin(), parts.end(), [&](const auto &p) { return p.name == part; }) - parts.begin();
    if (index == static_cast<std::ptrdiff_t>(parts.size())) {
      parts.push_back({part, codec, level, {}});
      auto spec = common;
      spec.nchannels = 0;
      spec.channelnames.clear();
      spec.channelformats.clear();
      spec.attribute("compression", (codec == "dwaa" || codec == "dwab") ? codec + ":" + std::to_string(level) : codec);
      if (options.multipart)
        spec.attribute("oiio:subimagename", part);
      specs.push_back(std::move(spec));
    }
    auto &target = parts[index];
    auto &spec = specs[index];
    if (target.compression != codec || target.dwa_level != level)
      throw std::invalid_argument("Layers sharing one EXR part must use matching compression: " + part);
    const bool half = selection.precision == "half" || (selection.precision == "inherit" && options.half_color);
    // A standalone pass is the file's primary image. Use RGB for vector
    // components as well: bare XYZ is interpreted as Y luminance by RGBA
    // readers. A scalar uses standard luminance Y. Retain a fourth data
    // component (e.g. motion W) without inventing coverage alpha. Shared
    // files and Cryptomatte keep their original component/layer names.
    const bool standalone = !options.multipart && selected.size() == 1 && !d.cryptomatte;
    for (std::size_t component = 0; component < d.channels.size(); ++component) {
      const auto &channel = d.channels[component];
      const auto name = standalone      ? (d.channels.size() == 1 ? std::string("Y")
                                           : component < 3        ? std::string(1, "RGB"[component])
                                                                  : channel)
                        : layer.empty() ? channel
                                        : layer + "." + channel;
      if (std::find(spec.channelnames.begin(), spec.channelnames.end(), name) != spec.channelnames.end())
        throw std::invalid_argument("Duplicate output channel: " + name);
      spec.channelnames.push_back(name);
      spec.channelformats.push_back(half ? OIIO::TypeDesc::HALF : OIIO::TypeDesc::FLOAT);
      ++spec.nchannels;
    }
    if (d.cryptomatte) {
      const auto source = found->pass.substr(0, found->pass.size() - 2), root = layer.substr(0, layer.size() - 2);
      const auto before = "cryptomatte/" + crypto_hash(source).substr(0, 7) + "/",
                 after = "cryptomatte/" + crypto_hash(root).substr(0, 7) + "/";
      bool manifest = false;
      if (image->output_metadata)
        for (const auto &[name, value] : *image->output_metadata)
          if (name.starts_with(before)) {
            const auto key = name.substr(before.size());
            spec.attribute(after + key, key == "name" ? root : value);
            manifest |= key == "manifest";
          }
      if (!manifest)
        throw std::logic_error("Missing Cryptomatte manifest: " + source);
    }
    target.planes.push_back(&*found);
  }
  if (parts.empty())
    throw std::runtime_error("No selected output passes");
  for (auto &spec : specs) {
    const auto alpha = std::find(spec.channelnames.begin(), spec.channelnames.end(), "A");
    spec.alpha_channel = alpha == spec.channelnames.end() ? -1 : static_cast<int>(alpha - spec.channelnames.begin());
  }
  // The same contract guards an existing file before encoding and a winner
  // that arrives during encoding. Header readability alone never accepts a
  // directory, a different image size/window, or an incomplete pass set.
  const auto validate_existing = [&] {
    const auto path = std::filesystem::u8path(filename);
    if (!std::filesystem::is_regular_file(std::filesystem::symlink_status(path)))
      throw std::runtime_error("Existing output is not a regular file: " + filename);
    OIIO::Filesystem::IOFile file(filename, OIIO::Filesystem::IOProxy::Read);
    OIIO::ImageSpec hints;
    hints.attribute("oiio:missingcolor", "");
    auto input = OIIO::ImageInput::open(filename, &hints, &file);
    if (!input) {
      const auto error = OIIO::geterror();
      throw std::runtime_error("Existing output is not a readable EXR: " + filename + ": " + error);
    }
    if (std::string(input->format_name()) != "openexr")
      throw std::runtime_error("Existing output is not an EXR: " + filename);
    std::set<std::string> seen_parts;
    std::vector<OIIO::ImageSpec> actual_specs;
    for (std::size_t index = 0; index < specs.size(); ++index) {
      OIIO::ImageSpec actual;
      if (!input->seek_subimage(int(index), 0, actual))
        throw std::runtime_error("Existing EXR is missing a part: " + filename + ": " + input->geterror());
      const auto name = actual.get_string_attribute("oiio:subimagename");
      auto expected_part =
          options.multipart
              ? std::find_if(specs.begin(), specs.end(),
                             [&](const auto &spec) { return spec.get_string_attribute("oiio:subimagename") == name; })
              : specs.begin();
      if (expected_part == specs.end() || (options.multipart && !seen_parts.insert(std::string(name)).second))
        throw std::runtime_error("Existing EXR has unexpected or duplicate parts: " + filename);
      const auto &expected = *expected_part;
      if (options.fail_on_missing_inputs) {
        if (actual.get_int_attribute("cycles_houdini:input_validation_version", 0) != 1 ||
            actual.get_int_attribute("cycles_houdini:missing_input_count", -1) < 0 ||
            actual.get_string_attribute("cycles_houdini:source_diagnostics").empty())
          throw std::runtime_error(
              "Existing EXR has uncertified render-input provenance; use Overwrite to replace it: " + filename);
        if (actual.get_int_attribute("cycles_houdini:missing_input_count", -1) != 0)
          throw std::runtime_error(
              "Existing EXR contains missing render inputs; use Overwrite after repairing inputs: " + filename + ": " +
              std::string(actual.get_string_attribute("cycles_houdini:source_diagnostics")));
      }
      auto channels = actual.channelnames, wanted = expected.channelnames;
      std::sort(channels.begin(), channels.end());
      std::sort(wanted.begin(), wanted.end());
      if (actual.x != expected.x || actual.y != expected.y || actual.z != expected.z ||
          actual.width != expected.width || actual.height != expected.height || actual.depth != expected.depth ||
          actual.full_x != expected.full_x || actual.full_y != expected.full_y || actual.full_z != expected.full_z ||
          actual.full_width != expected.full_width || actual.full_height != expected.full_height ||
          actual.full_depth != expected.full_depth || channels != wanted ||
          (options.multipart &&
           actual.get_string_attribute("oiio:subimagename") != expected.get_string_attribute("oiio:subimagename")))
        throw std::runtime_error("Existing EXR dimensions, windows, channels or parts do not match: " + filename);
      actual_specs.push_back(std::move(actual));
    }
    OIIO::ImageSpec extra;
    if (input->seek_subimage(int(specs.size()), 0, extra))
      throw std::runtime_error("Existing EXR has unexpected parts: " + filename);
    input->geterror(); // Expected end-of-parts probe must not leave a pending OIIO error.
    check_exr_payload(file, *input, actual_specs, filename);
    if (!input->close())
      throw std::runtime_error("Cannot close existing EXR: " + filename + ": " + input->geterror());
  };
  if (options.existing_files != "overwrite" &&
      std::filesystem::exists(std::filesystem::symlink_status(std::filesystem::u8path(filename)))) {
    if (options.existing_files == "error")
      throw std::runtime_error("Render output already exists: " + filename);
    validate_existing();
    return;
  }
  // All headers/resources are resolved before encoding. A reusable scanline
  // band bounds assembly memory even for 4K files with many AOVs.
  StagedOutput staged(filename);
  const auto temporary = staged.temporary_filename();
  auto output = OIIO::ImageOutput::create(temporary);
  if (!output)
    throw std::runtime_error("Could not create output: " + filename + ": " + OIIO::geterror());
  const bool opened = options.multipart ? output->open(temporary, int(specs.size()), specs.data())
                                        : output->open(temporary, specs.front());
  if (!opened)
    throw std::runtime_error(output->geterror());
  std::vector<float> interleaved;
  for (std::size_t index = 0; index < parts.size(); ++index) {
    const auto &part = parts[index];
    const auto &spec = specs[index];
    const int channels = spec.nchannels;
    if (index && !output->open(temporary, spec, OIIO::ImageOutput::AppendSubimage))
      throw std::runtime_error(output->geterror());
    // A 64 MiB band lets the codec compress several blocks concurrently.
    // Align ordinary bands to codec blocks, but never round up to DWAB's
    // 256 rows: wide/many-AOV images must keep the same memory bound.
    // A single row can exceed the budget; codec-owned buffering is separate.
    const auto row_values = static_cast<std::size_t>(image->width) * channels;
    int band_height = static_cast<int>(std::min<std::size_t>(
        image->height, std::max<std::size_t>(1, (64u * 1024u * 1024u) / (row_values * sizeof(float)))));
    const int block = part.compression == "dwab" ? 256
                      : (part.compression == "piz" || part.compression == "b44" || part.compression == "b44a" ||
                         part.compression == "dwaa")
                          ? 32
                      : (part.compression == "zip" || part.compression == "pxr24") ? 16
                                                                                   : 1;
    if (band_height >= block)
      band_height -= band_height % block;
    interleaved.resize(row_values * band_height);
    for (int first = 0; first < image->height; first += band_height) {
      const int rows = std::min(band_height, image->height - first);
      for (int y = 0; y < rows; ++y)
        for (int x = 0; x < image->width; ++x) {
          const auto from = static_cast<std::size_t>(image->height - 1 - first - y) * image->width + x;
          auto *to = interleaved.data() + (static_cast<std::size_t>(y) * image->width + x) * channels;
          for (const auto *p : part.planes) {
            std::copy_n(p->pixels.data() + from * p->channels, p->channels, to);
            to += p->channels;
          }
        }
      if (processor && !processor->isNoOp()) {
        std::size_t offset = 0;
        const auto count = std::size_t(image->width) * rows;
        for (const auto *p : part.planes) {
          const auto &d = descriptor(p->pass);
          if (d.color && (p->channels == 3 || p->channels == 4)) {
            if (p->channels == 4)
              for (std::size_t i = 0; i < count; ++i) {
                auto *rgb = interleaved.data() + i * channels + offset;
                const float a = rgb[3];
                if (a != 0)
                  for (int c = 0; c < 3; ++c)
                    rgb[c] /= a;
              }
            OCIO_NAMESPACE::PackedImageDesc pixels(
                interleaved.data() + offset, image->width, rows, 3, OCIO_NAMESPACE::BIT_DEPTH_F32, sizeof(float),
                channels * sizeof(float), std::ptrdiff_t(image->width) * channels * sizeof(float));
            processor->apply(pixels);
            if (p->channels == 4)
              for (std::size_t i = 0; i < count; ++i) {
                auto *rgb = interleaved.data() + i * channels + offset;
                const float a = rgb[3];
                if (a != 0)
                  for (int c = 0; c < 3; ++c)
                    rgb[c] *= a;
              }
          }
          offset += p->channels;
        }
      }
      if (!output->write_scanlines(spec.y + first, spec.y + first + rows, 0, OIIO::TypeDesc::FLOAT, interleaved.data()))
        throw std::runtime_error("EXR write failed: " + output->geterror());
    }
  }
  if (!output->close())
    throw std::runtime_error("EXR close failed: " + output->geterror());
  if (!staged.commit(options.existing_files == "overwrite")) {
    if (options.existing_files == "error")
      throw std::runtime_error("Render output already exists: " + filename);
    validate_existing();
  }
}
} // namespace cycles_houdini
