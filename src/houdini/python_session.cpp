// SPDX-License-Identifier: Apache-2.0
#include "python_binding_state.h"
#include <pybind11/stl.h>
#include <chrono>
#include <cmath>
#include <limits>
#include "python_status.h"
#include "texture_policy.h"
#include "material_vop.h"
#include "material_subnet.h"
#include "material_preview.h"
#include "blend_mesh.h"
#include "scene_persistence.h"
#include "packed_fragments.h"
#include "../core/color_internal.h"
#include "../core/shader_curves.h"
#include "../core/owned_ramp.h"
#include "../core/render_settings.h"
#include "../core/camera_motion.h"
#include "cycles_houdini/texture_cache.h"
#include "cycles_houdini/memory.h"
#include <functional>
#include <iomanip>
#include <sstream>
#include <type_traits>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimPacked.h>
#include <GA/GA_Iterator.h>
#include <OP/OP_Context.h>
#include <OP/OP_Director.h>
#include <OP/OP_Network.h>
#include <OP/OP_Node.h>
#include <OP/OP_Operator.h>
#include <UT/UT_Thread.h>
#include <VOP/VOP_Node.h>
#include <VOP/VOP_OutputInfoManager.h>

namespace cycles_houdini::python_detail {
using CopImagePtr = std::shared_ptr<const ch::NativeCopImage>;

struct RampPreview {
  std::optional<ch::OwnedRampTable> color, alpha;
  std::string color_issue, alpha_issue;
  std::array<float, 4> sample(float factor) const {
    const auto rgb = !color ? std::array<float, 3>{1, 0, 1} : ch::evaluate_owned_ramp(*color, factor);
    return {rgb[0], rgb[1], rgb[2], !alpha ? 1.f : ch::evaluate_owned_ramp(*alpha, factor)[0]};
  }
};

CopImagePtr cop_image(py::handle value);

ch::ExtractionStats capture_stats(const Handle &h) {
  auto result = h.adapter.stats();
  result.lifecycle.retired_snapshots = h.retired_snapshot ? 1 : 0;
  result.lifecycle.retired_snapshot_objects = h.retired_snapshot ? h.retired_snapshot->objects->size() : 0;
  return result;
}

// Python handles remain typed and owning values stay in pybind11 objects.
// The two narrow CPython storage operations below document their contracts.
// pybind11 exposes immutable bytes. Large output packets are allocated once,
// filled before publication while privately owned, then exposed as bytes.
// This is the sole writable-bytes CPython interop; never pass a published value.
char *unpublished_bytes_data(py::handle value) {
  auto *data = PyBytes_AsString(value.ptr());
  if (!data)
    throw py::error_already_set();
  return data;
}

const char *c_string_checked(const std::string &value) {
  if (value.find('\0') != std::string::npos)
    throw py::value_error("embedded null character");
  return value.c_str();
}

py::capsule omitted() {
  static int marker;
  return py::capsule(&marker, "cycles_houdini.OmittedArgument");
}

bool is_omitted(py::handle value) {
  if (!py::isinstance<py::capsule>(value))
    return false;
  auto capsule = py::reinterpret_borrow<py::capsule>(value);
  return capsule.name() && std::strcmp(capsule.name(), "cycles_houdini.OmittedArgument") == 0 &&
         capsule.get_pointer() == omitted().get_pointer();
}

bool truth(py::handle value) { return py::cast<bool>(py::bool_(py::reinterpret_borrow<py::object>(value))); }

Handle &handle(py::handle obj) {
  return *capsule_pointer<Handle>(obj, "cycles_houdini.RenderSession", "Invalid or closed Cycles session handle");
}

SnapshotPtr snapshot(py::handle value) {
  return *capsule_pointer<SnapshotPtr>(value, "cycles_houdini.SceneSnapshot", "Invalid owned scene snapshot");
}

py::object item(py::handle dictionary, const char *key, bool required) {
  if (!dictionary || !py::isinstance<py::dict>(dictionary))
    throw std::invalid_argument("Expected a dictionary");
  // Python 3.13's owned dictionary-storage lookup deliberately bypasses a
  // subclass __getitem__. pybind11's generic item accessor dispatches that
  // override; a callback could return a transient value or mutate its parent.
  // Take ownership immediately, retaining values through later conversions.
  PyObject *value = nullptr;
  const int found = PyDict_GetItemStringRef(dictionary.ptr(), key, &value);
  if (found < 0)
    throw py::error_already_set();
  if (!found) {
    if (required)
      throw std::invalid_argument(std::string("Missing field: ") + key);
    return {};
  }
  return py::reinterpret_steal<py::object>(value);
}

std::string string(py::handle object) {
  if (!object || !py::isinstance<py::str>(object))
    throw std::invalid_argument("Expected a string");
  std::string text;
  try {
    text = py::cast<std::string>(object);
  } catch (py::error_already_set &) {
    throw std::invalid_argument("Invalid UTF-8 string");
  }
  // Legacy generic strings use C-string semantics; shader strings below
  // explicitly reject embedded NUL instead.
  return text.substr(0, text.find('\0'));
}

double number(py::handle object) {
  const auto owner = py::reinterpret_borrow<py::object>(object);
  try {
    return py::cast<double>(owner);
  } catch (const py::cast_error &) {
    throw std::invalid_argument("Expected a number");
  } catch (py::error_already_set &) {
    throw std::invalid_argument("Expected a number");
  }
}

ch::DenoiseSettings denoise_settings(py::handle value) {
  if (!py::isinstance<py::dict>(value))
    throw std::invalid_argument("Denoising requires a settings dictionary");
  ch::DenoiseSettings result;
  const std::set<std::string> fields = {"enabled",   "method",       "use_gpu", "quality",
                                        "prefilter", "start_sample", "passes",  "upscale_factor"};
  for (auto [key_handle, entry_handle] : py::reinterpret_borrow<py::dict>(value))
    if (!fields.contains(string(key_handle)))
      throw std::invalid_argument("Unknown denoising setting: " + string(key_handle));
  for (auto [name, target] : {std::pair{"enabled", &result.enabled}, std::pair{"use_gpu", &result.use_gpu}})
    if (auto item_value = item(value, name, false)) {
      if (!py::isinstance<py::bool_>(item_value))
        throw std::invalid_argument(std::string("Denoising ") + name + " requires a boolean");
      *target = truth(item_value);
    }
  for (auto [name, target] : {std::pair{"method", &result.method}, std::pair{"quality", &result.quality},
                              std::pair{"prefilter", &result.prefilter}, std::pair{"passes", &result.passes}})
    if (auto item_value = item(value, name, false))
      *target = string(item_value);
  if (auto factor = item(value, "upscale_factor", false))
    result.upscale_factor = float(number(factor));
  if (auto sample = item(value, "start_sample", false)) {
    if (!py::isinstance<py::int_>(sample) || py::isinstance<py::bool_>(sample))
      throw std::invalid_argument("Denoising start_sample requires an integer");
    const auto count = integer<long long>(sample, "Denoising start_sample must fit a nonnegative signed integer");
    if (count < 0 || count > std::numeric_limits<int>::max())
      throw std::invalid_argument("Denoising start_sample must fit a nonnegative signed integer");
    result.start_sample = int(count);
  }
  return result;
}

ch::CryptomatteSettings cryptomatte_settings(py::handle value) {
  if (!py::isinstance<py::dict>(value))
    throw std::invalid_argument("Cryptomatte requires a settings dictionary");
  ch::CryptomatteSettings result;
  for (auto [key_handle, entry_handle] : py::reinterpret_borrow<py::dict>(value)) {
    const auto name = string(key_handle);
    if (name != "object" && name != "material" && name != "asset" && name != "levels")
      throw std::invalid_argument("Unknown Cryptomatte setting: " + name);
  }
  for (auto [name, target] : {std::pair{"object", &result.object}, std::pair{"material", &result.material},
                              std::pair{"asset", &result.asset}})
    if (auto field = item(value, name, false)) {
      if (!py::isinstance<py::bool_>(field))
        throw std::invalid_argument("Cryptomatte switches require booleans");
      *target = truth(field);
    }
  if (auto field = item(value, "levels", false)) {
    if (!py::isinstance<py::int_>(field) || py::isinstance<py::bool_>(field))
      throw std::invalid_argument("Cryptomatte levels must be an integer");
    const auto levels = integer<long long>(field, "Cryptomatte levels must be within 2..16");
    if (levels < 2 || levels > 16)
      throw std::invalid_argument("Cryptomatte levels must be within 2..16");
    result.levels = int(levels);
  }
  ch::validate_cryptomatte(result);
  return result;
}

ch::RenderSettings render_settings(py::handle value, int samples) {
  if (!py::isinstance<py::dict>(value))
    throw std::invalid_argument("Rendering requires a settings dictionary");
  ch::RenderSettings result;
  result.samples = samples;
  std::set<std::string> fields;
  const auto set_integer = [&](const char *name, int &target) {
    fields.insert(name);
    if (auto v = item(value, name, false)) {
      if (!py::isinstance<py::int_>(v) || py::isinstance<py::bool_>(v))
        throw std::invalid_argument(std::string(name) + " requires an integer");
      const auto n = integer<long long>(v, (std::string(name) + " is outside integer range").c_str());
      if (n < std::numeric_limits<int>::min() || n > std::numeric_limits<int>::max())
        throw std::invalid_argument(std::string(name) + " is outside integer range");
      target = int(n);
    }
  };
  const auto real = [&](const char *name, auto &target) {
    fields.insert(name);
    if (auto v = item(value, name, false))
      target = static_cast<std::remove_reference_t<decltype(target)>>(number(v));
  };
  const auto boolean = [&](const char *name, bool &target) {
    fields.insert(name);
    if (auto v = item(value, name, false)) {
      if (!py::isinstance<py::bool_>(v))
        throw std::invalid_argument(std::string(name) + " requires a boolean");
      target = truth(v);
    }
  };
  const auto token = [&](const char *name, std::string &target) {
    fields.insert(name);
    if (auto v = item(value, name, false))
      target = string(v);
  };
#define CH_INT(name) set_integer(#name, result.name)
#define CH_REAL(name) real(#name, result.name)
#define CH_BOOL(name) boolean(#name, result.name)
#define CH_TOKEN(name) token(#name, result.name)
  CH_INT(samples);
  CH_BOOL(adaptive_sampling);
  CH_REAL(noise_threshold);
  CH_INT(min_samples);
  CH_REAL(time_limit);
  CH_TOKEN(sampling_pattern);
  CH_INT(seed);
  CH_BOOL(animate_seed);
  CH_REAL(frame);
  CH_INT(min_bounce);
  CH_INT(max_bounce);
  CH_INT(diffuse_bounce);
  CH_INT(glossy_bounce);
  CH_INT(transmission_bounce);
  CH_INT(volume_bounce);
  CH_INT(transparent_min_bounce);
  CH_INT(transparent_max_bounce);
  CH_BOOL(light_tree);
  CH_REAL(light_threshold);
  CH_REAL(clamp_direct);
  CH_REAL(clamp_indirect);
  CH_REAL(filter_glossy);
  CH_REAL(glossy_lobe_floor);
  CH_BOOL(resampled_light_sampling);
  CH_BOOL(use_guiding);
  CH_BOOL(deterministic_guiding);
  CH_BOOL(nested_dielectrics);
  CH_BOOL(caustics_reflective);
  CH_BOOL(caustics_refractive);
  CH_BOOL(fast_gi);
  CH_TOKEN(fast_gi_method);
  CH_INT(ao_bounces);
  CH_REAL(ao_factor);
  CH_REAL(ao_distance);
  CH_BOOL(volume_biased);
  CH_REAL(volume_step_rate);
  CH_INT(volume_max_steps);
  CH_REAL(film_exposure);
  CH_REAL(filter_width);
  CH_REAL(pass_alpha_threshold);
  CH_REAL(mist_start);
  CH_REAL(mist_depth);
  CH_REAL(mist_falloff);
  CH_TOKEN(pixel_filter);
  CH_BOOL(sample_subset);
  CH_INT(subset_offset);
  CH_INT(subset_length);
#undef CH_INT
#undef CH_REAL
#undef CH_BOOL
#undef CH_TOKEN
  for (auto [key_handle, entry_handle] : py::reinterpret_borrow<py::dict>(value))
    if (!fields.contains(string(key_handle)))
      throw std::invalid_argument("Unknown rendering setting: " + string(key_handle));
  return result;
}

py::dict rendering_value(const ch::RenderSettings &s) {
  py::dict result;
  const auto insert = [&](const char *name, auto &&value) { result[name] = py::cast(value); };
#define CH_INT(name) insert(#name, s.name)
#define CH_REAL(name) insert(#name, s.name)
#define CH_BOOL(name) insert(#name, bool(s.name))
#define CH_TOKEN(name) insert(#name, s.name)
  CH_INT(samples);
  CH_BOOL(adaptive_sampling);
  CH_REAL(noise_threshold);
  CH_INT(min_samples);
  CH_REAL(time_limit);
  CH_TOKEN(sampling_pattern);
  CH_INT(seed);
  CH_BOOL(animate_seed);
  CH_REAL(frame);
  CH_INT(min_bounce);
  CH_INT(max_bounce);
  CH_INT(diffuse_bounce);
  CH_INT(glossy_bounce);
  CH_INT(transmission_bounce);
  CH_INT(volume_bounce);
  CH_INT(transparent_min_bounce);
  CH_INT(transparent_max_bounce);
  CH_BOOL(light_tree);
  CH_REAL(light_threshold);
  CH_REAL(clamp_direct);
  CH_REAL(clamp_indirect);
  CH_REAL(filter_glossy);
  CH_REAL(glossy_lobe_floor);
  CH_BOOL(resampled_light_sampling);
  CH_BOOL(use_guiding);
  CH_BOOL(deterministic_guiding);
  CH_BOOL(nested_dielectrics);
  CH_BOOL(caustics_reflective);
  CH_BOOL(caustics_refractive);
  CH_BOOL(fast_gi);
  CH_TOKEN(fast_gi_method);
  CH_INT(ao_bounces);
  CH_REAL(ao_factor);
  CH_REAL(ao_distance);
  CH_BOOL(volume_biased);
  CH_REAL(volume_step_rate);
  CH_INT(volume_max_steps);
  CH_REAL(film_exposure);
  CH_REAL(filter_width);
  CH_REAL(pass_alpha_threshold);
  CH_REAL(mist_start);
  CH_REAL(mist_depth);
  CH_REAL(mist_falloff);
  CH_TOKEN(pixel_filter);
  CH_BOOL(sample_subset);
  CH_INT(subset_offset);
  CH_INT(subset_length);
#undef CH_INT
#undef CH_REAL
#undef CH_BOOL
#undef CH_TOKEN
  return result;
}

void denoise_metadata(py::dict result, const std::string &backend, const std::string &device) {
  result["denoiser"] = backend;
  result["denoise_device_id"] = device;
}

// Snapshot owning entries before callbacks can mutate their original list.
std::vector<py::object> sequence(py::handle object) {
  if (!object || (!py::isinstance<py::list>(object) && !py::isinstance<py::tuple>(object)))
    throw std::invalid_argument("Expected a list or tuple");
  std::vector<py::object> result;
  const auto copy = [&](const auto &container) {
    result.reserve(container.size());
    for (py::size_t index = 0; index < container.size(); ++index)
      result.emplace_back(container[index]);
  };
  // Typed access bypasses list/tuple subclass overrides, as the old storage
  // lookup did, while every returned element immediately gains ownership.
  if (py::isinstance<py::list>(object))
    copy(py::reinterpret_borrow<py::list>(object));
  else
    copy(py::reinterpret_borrow<py::tuple>(object));
  return result;
}

ch::Float3 vector3(py::handle object) {
  auto values = sequence(object);
  if (values.size() != 3)
    throw std::invalid_argument("Expected three vector components");
  return {float(number(values[0])), float(number(values[1])), float(number(values[2]))};
}

ch::Matrix matrix(py::handle object) {
  auto values = sequence(object);
  if (values.size() != 12)
    throw std::invalid_argument("Expected a 3x4 column-vector transform");
  ch::Matrix m{};
  for (int i = 0; i < 12; ++i)
    m[i] = float(number(values[i]));
  return m;
}

std::string shader_string(py::handle object) {
  if (!object || !py::isinstance<py::str>(object))
    throw std::invalid_argument("Shader graph requires string identifiers/enum tokens");
  std::string result;
  try {
    result = py::cast<std::string>(object);
  } catch (py::error_already_set &) {
    throw std::invalid_argument("Invalid UTF-8 shader string");
  }
  if (result.find('\0') != std::string::npos)
    throw std::invalid_argument("Embedded NUL in shader graph string");
  return result;
}

std::vector<ch::AovDefinition> aov_definitions(py::handle value) {
  std::vector<ch::AovDefinition> result;
  for (auto row : sequence(value)) {
    const auto name = shader_string(item(row, "name"));
    const auto type = shader_string(item(row, "type"));
    if (py::len(row) != 2)
      throw std::invalid_argument("Shader AOV entries require only name and type");
    if (type != "color" && type != "value")
      throw std::invalid_argument("Shader AOV type must be color or value");
    result.push_back({name, type == "color"});
  }
  ch::validate_aovs(result);
  return result;
}

std::vector<std::string> lightgroup_definitions(py::handle value) {
  if (!py::isinstance<py::list>(value) && !py::isinstance<py::tuple>(value))
    throw std::invalid_argument("Light groups require a list of names");
  std::vector<std::string> result;
  for (auto entry : sequence(value))
    result.push_back(shader_string(entry));
  ch::validate_lightgroups(result);
  return result;
}

ch::ShaderEndpoint shader_endpoint(py::handle value) {
  return {shader_string(item(value, "node")), shader_string(item(value, "socket"))};
}

ch::ShaderValue shader_value(py::handle value, bool integer_array, bool ramp_controls, int control_limit) {
  if (ramp_controls) {
    if ((!py::isinstance<py::list>(value) && !py::isinstance<py::tuple>(value)) || py::len(value) > control_limit)
      throw std::invalid_argument("Owned shader controls exceed their bounded signed-word limit");
    std::vector<int> result;
    for (auto entry : sequence(value)) {
      if (!py::isinstance<py::int_>(entry) || py::isinstance<py::bool_>(entry))
        throw std::invalid_argument("Owned ramp control words require integers");
      const auto word = integer<long long>(entry, "Owned ramp control word is outside signed 32-bit range");
      if (word < std::numeric_limits<int>::min() || word > std::numeric_limits<int>::max())
        throw std::invalid_argument("Owned ramp control word is outside signed 32-bit range");
      result.push_back(int(word));
    }
    return result;
  }
  if (integer_array) {
    if ((!py::isinstance<py::list>(value) && !py::isinstance<py::tuple>(value)) || py::len(value) > 8999)
      throw std::invalid_argument("Texture tiles require a list of at most 8999 integers");
    std::vector<int> result;
    for (auto entry : sequence(value)) {
      if (!py::isinstance<py::int_>(entry) || py::isinstance<py::bool_>(entry))
        throw std::invalid_argument("Texture tile IDs require integers");
      const auto tile = integer<long>(entry, "Texture tile IDs must be in 1001..9999");
      if (tile < 1001 || tile > 9999)
        throw std::invalid_argument("Texture tile IDs must be in 1001..9999");
      result.push_back(int(tile));
    }
    return result;
  }
  if (py::isinstance<py::bool_>(value))
    return truth(value);
  if (py::isinstance<py::int_>(value)) {
    const auto result = integer<long>(value, "Shader integer is outside int32 range");
    if (result < std::numeric_limits<int>::min() || result > std::numeric_limits<int>::max())
      throw std::invalid_argument("Shader integer is outside int32 range");
    return int(result);
  }
  if (py::isinstance<py::float_>(value))
    return float(number(value));
  if (py::isinstance<py::str>(value))
    return shader_string(value);
  if (py::isinstance<py::tuple>(value) || py::isinstance<py::list>(value)) {
    const auto v = vector3(value);
    return std::array<float, 3>{v.x, v.y, v.z};
  }
  throw std::invalid_argument("Shader values require bool, int32, float, float3 or string");
}

ch::ShaderGraphData shader_graph(py::handle value) {
  ch::ShaderGraphData result;
  if (auto time = item(value, "image_time", false))
    result.image_time = number(time);
  if (auto version = item(value, "schema_version", false)) {
    if (!py::isinstance<py::int_>(version) || py::isinstance<py::bool_>(version))
      throw std::invalid_argument("Shader graph schema version requires an integer");
    const auto parsed = integer<long>(version, "Unsupported shader graph schema version");
    if (parsed != 1)
      throw std::invalid_argument("Unsupported shader graph schema version");
  }
  auto nodes = item(value, "nodes");
  if ((!py::isinstance<py::list>(nodes) && !py::isinstance<py::tuple>(nodes)) || py::len(nodes) > 256)
    throw std::invalid_argument("Shader graph supports 1..256 nodes");
  for (auto data : sequence(nodes)) {
    ch::ShaderNodeData node;
    node.id = shader_string(item(data, "id"));
    node.definition = shader_string(item(data, "definition"));
    if (auto parameters = item(data, "parameters", false)) {
      if (!py::isinstance<py::dict>(parameters))
        throw std::invalid_argument("Shader parameters require a dictionary");
      for (auto [key, parameter] : py::reinterpret_borrow<py::dict>(parameters)) {
        const auto name = shader_string(key);
        if (node.definition == "cycles/owned_ramp/1" && name == "program")
          throw std::invalid_argument("Owned ramp programs are private worker-prepared resources");
        const bool curve = name == "curve_source" &&
                           (node.definition == "cycles/float_curve/1" || node.definition == "cycles/rgb_curves/1" ||
                            node.definition == "cycles/vector_curves/1");
        node.parameters.emplace(
            name, shader_value(parameter, node.definition == "cycles/image_texture/1" && name == "tiles",
                               curve || (node.definition == "cycles/owned_ramp/1" && name == "source"), 780));
      }
    }
    result.nodes.push_back(std::move(node));
  }
  if (auto list = item(value, "links", false)) {
    if ((!py::isinstance<py::list>(list) && !py::isinstance<py::tuple>(list)) || py::len(list) > 2048)
      throw std::invalid_argument("Shader graph supports at most 2048 links");
    for (auto link : sequence(list))
      result.links.push_back({shader_endpoint(item(link, "from")), shader_endpoint(item(link, "to"))});
  }
  if (auto output = item(value, "surface", false))
    result.surface = shader_endpoint(output);
  if (auto output = item(value, "volume", false))
    result.volume = shader_endpoint(output);
  if (auto output = item(value, "displacement", false))
    result.displacement = shader_endpoint(output);
  if (auto animated = item(value, "displacement_time_dependent", false)) {
    if (!py::isinstance<py::bool_>(animated))
      throw std::invalid_argument("Displacement time dependency requires a boolean");
    result.displacement_time_dependent = truth(animated);
  }
  if (auto method = item(value, "displacement_method", false)) {
    const auto name = string(method);
    if (name == "bump")
      result.displacement_method = ch::DisplacementMethod::Bump;
    else if (name == "true")
      result.displacement_method = ch::DisplacementMethod::True;
    else if (name == "both")
      result.displacement_method = ch::DisplacementMethod::Both;
    else
      throw std::invalid_argument("Unknown displacement method: " + name);
  }
  if (auto settings = item(value, "settings", false)) {
    if (!py::isinstance<py::dict>(settings))
      throw std::invalid_argument("Material settings require a dictionary");
    auto &out = result.settings;
    for (auto [key, entry] : py::reinterpret_borrow<py::dict>(settings)) {
      const auto name = string(key);
      const auto choice = [&](std::initializer_list<const char *> tokens) {
        const auto token = string(entry);
        int index = 0;
        for (const char *candidate : tokens) {
          if (token == candidate)
            return index;
          ++index;
        }
        throw std::invalid_argument("Unknown material " + name + ": " + token);
      };
      const auto toggle = [&] {
        if (!py::isinstance<py::bool_>(entry) && !py::isinstance<py::int_>(entry))
          throw std::invalid_argument("Material " + name + " requires a boolean");
        return truth(entry);
      };
      if (name == "emission_sampling")
        out.emission_sampling = ch::EmissionSampling(choice({"none", "auto", "front", "back", "front_back"}));
      else if (name == "transparent_shadows")
        out.transparent_shadows = toggle();
      else if (name == "bump_map_correction")
        out.bump_map_correction = toggle();
      else if (name == "volume_sampling")
        out.volume_sampling = ch::VolumeSampling(choice({"distance", "equiangular", "multiple_importance"}));
      else if (name == "volume_interpolation")
        out.volume_interpolation = ch::VolumeInterpolation(choice({"linear", "cubic"}));
      else if (name == "volume_step_rate") {
        const auto rate = number(entry);
        if (!std::isfinite(rate) || rate < 0.001 || rate > 1000)
          throw std::invalid_argument("Material volume step rate requires a value within 0.001..1000");
        out.volume_step_rate = float(rate);
      } else
        throw std::invalid_argument("Unknown material setting: " + name);
    }
  }
  return result;
}

int pass_index(py::handle value) {
  if (!py::isinstance<py::int_>(value) || py::isinstance<py::bool_>(value))
    throw std::invalid_argument("Pass Index requires an integer within 0..32767");
  const auto index = integer<long long>(value, "Pass Index requires an integer within 0..32767");
  if (index < 0 || index > 32767)
    throw std::invalid_argument("Pass Index requires an integer within 0..32767");
  return int(index);
}

int dielectric_priority(py::handle value) {
  if (!py::isinstance<py::int_>(value) || py::isinstance<py::bool_>(value))
    throw std::invalid_argument("Dielectric Priority requires an integer");
  const auto priority = integer<long long>(value, "Dielectric Priority requires an integer within -16777216..16777216");
  if (priority < -ch::dielectric_priority_limit || priority > ch::dielectric_priority_limit)
    throw std::invalid_argument("Dielectric Priority requires an integer within -16777216..16777216");
  return int(priority);
}

int world_integer(py::handle value) {
  if (!py::isinstance<py::int_>(value) || py::isinstance<py::bool_>(value))
    throw std::invalid_argument("World settings require integer values");
  const auto result = integer<long long>(value, "World integer is outside its supported range");
  if (result < std::numeric_limits<int>::min() || result > std::numeric_limits<int>::max())
    throw std::invalid_argument("World integer is outside its supported range");
  return int(result);
}

std::vector<ch::SourceDiagnostic> source_issues(py::handle values) {
  std::vector<ch::SourceDiagnostic> result;
  for (auto value : sequence(values)) {
    ch::SourceDiagnostic issue;
    issue.code = string(item(value, "code"));
    issue.source = string(item(value, "source"));
    issue.message = string(item(value, "message"));
    issue.action = string(item(value, "action"));
    if (auto resource = item(value, "resource", false))
      issue.resource = string(resource);
    if (auto severity = item(value, "severity", false))
      issue.severity = string(severity);
    if (auto attribute = item(value, "attribute", false))
      issue.attribute = string(attribute);
    result.push_back(std::move(issue));
  }
  return result;
}

ch::MaterialData material(py::handle value) {
  ch::MaterialData m;
  m.key = string(item(value, "key"));
  if (auto v = item(value, "color", false))
    m.color = vector3(v);
  if (auto v = item(value, "roughness", false))
    m.roughness = float(number(v));
  if (auto v = item(value, "metallic", false))
    m.metallic = float(number(v));
  if (auto v = item(value, "color_attribute", false))
    m.color_attribute = string(v);
  if (auto v = item(value, "cryptomatte_name", false))
    m.cryptomatte_name = shader_string(v);
  if (auto v = item(value, "pass_index", false))
    m.pass_index = pass_index(v);
  ch::validate_cryptomatte_name(m.cryptomatte_name);
  if (auto kind = item(value, "kind", false)) {
    const auto k = string(kind);
    if (k == "principled")
      m.kind = ch::MaterialKind::Principled;
    else if (k == "emission")
      m.kind = ch::MaterialKind::Emission;
    else if (k == "volume_absorption")
      m.kind = ch::MaterialKind::VolumeAbsorption;
    else if (k == "principled_volume")
      m.kind = ch::MaterialKind::PrincipledVolume;
    else if (k == "graph")
      m.kind = ch::MaterialKind::Graph;
    else if (k == "diagnostic_surface")
      m.kind = ch::MaterialKind::DiagnosticSurface;
    else if (k != "diffuse")
      throw std::invalid_argument("Unknown material kind: " + k);
  }
  if (auto v = item(value, "graph", false))
    m.graph = shader_graph(v);
  if (auto values = item(value, "displacement_samples", false)) {
    if ((!py::isinstance<py::list>(values) && !py::isinstance<py::tuple>(values)) || py::len(values) > 129)
      throw std::invalid_argument("Displacement shutter graphs require at most 129 samples");
    for (auto value : sequence(values))
      m.displacement_samples.push_back(ch::displacement_snapshot(shader_graph(value)));
  }
  if (auto v = item(value, "strength", false))
    m.emission_strength = float(number(v));
  if (auto v = item(value, "density", false))
    m.density = float(number(v));
  if (auto v = item(value, "anisotropy", false))
    m.anisotropy = float(number(v));
  if (auto v = item(value, "volume_emission_strength", false))
    m.volume_emission_strength = float(number(v));
  if (auto v = item(value, "volume_emission_color", false))
    m.volume_emission_color = vector3(v);
  if (auto v = item(value, "source_diagnostics", false))
    m.source_diagnostics = source_issues(v);
  if (auto v = item(value, "emission_field", false))
    m.emission_field = string(v);
  if (auto v = item(value, "blackbody_intensity", false))
    m.blackbody_intensity = float(number(v));
  if (auto v = item(value, "temperature_kelvin_scale", false))
    m.temperature_kelvin_scale = float(number(v));
  // Match the engine invariant before snapshot/texture preparation can walk
  // the graph. Rejection must retain the last accepted partial scene.
  if ((m.kind == ch::MaterialKind::Graph) != m.graph.has_value())
    throw std::invalid_argument("Graph materials require a graph and other material kinds cannot contain one");
  if (m.kind == ch::MaterialKind::Graph && !m.color_attribute.empty())
    throw std::invalid_argument("Shader graphs must author color attributes explicitly in their nodes");
  return m;
}

py::list material_reference_list(const std::vector<ch::NativeMaterialReference> &references) {
  py::list result;
  for (const auto &reference : references) {
    py::dict value;
    value["key"] = reference.key;
    value["path"] = reference.path;
    value["session_id"] = reference.session_id;
    value["identity"] = reference.identity;
    value["overrides"] = py::cast(reference.overrides);
    value["surface_overrides"] = py::cast(reference.surface_overrides);
    value["displacement_overrides"] = py::cast(reference.displacement_overrides);
    result.append(value);
  }
  return result;
}

std::string python_error_message(const py::error_already_set &error) {
  try {
    return py::str(error.value()).cast<std::string>();
  } catch (py::error_already_set &) {
    return "Python exception without a string representation";
  }
}

ch::MaterialData prepare_material_graph(py::handle resolver, const ch::NativeMaterialReference &reference,
                                        double time) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material graph preparation requires the host thread");
  if (!resolver || !py::isinstance<py::function>(resolver)) {
    // callable objects (including bound extension methods) also participate.
    if (!resolver || !py::module_::import("builtins").attr("callable")(resolver).cast<bool>())
      throw std::invalid_argument("Native material assignments require a callable material_resolver");
  }
  py::module_ source_module;
  try {
    source_module = py::module_::import("cycles_houdini.materials");
  } catch (py::error_already_set &error) {
    throw std::runtime_error("Cannot import native material source error type: " + python_error_message(error));
  }
  py::object source_type;
  try {
    source_type = source_module.attr("MaterialSourceError");
  } catch (py::error_already_set &error) {
    throw std::runtime_error("Missing native material source error type: " + python_error_message(error));
  }
  py::object resolved;
  try {
    auto references = material_reference_list({reference});
    resolved = py::reinterpret_borrow<py::object>(resolver)(references, time);
  } catch (py::error_already_set &error) {
    const auto message = "Native material resolution failed: " + python_error_message(error);
    if (error.matches(source_type))
      throw ch::NativeMaterialSourceError(message);
    throw std::runtime_error(message);
  }
  const auto values = sequence(resolved);
  if (values.empty())
    throw std::invalid_argument("Material resolver omitted a referenced native graph");
  if (values.size() != 1)
    throw std::invalid_argument("Material resolver must return exactly one graph for each referenced native key");
  return material(values.front());
}

void initialize(py::str path_arg, py::str cache_arg) {
  const auto path = py::cast<std::string>(path_arg);
  const auto cache = py::cast<std::string>(cache_arg);
  const auto *path_text = c_string_checked(path);
  const auto *cache_text = c_string_checked(cache);
  ReleaseGIL release;
  ch::initialize(path_text, cache_text);
}

py::list devices() {
  ch::Capabilities caps;
  {
    ReleaseGIL release;
    caps = ch::capabilities();
  }
  py::list result;
  for (const auto &d : caps.devices) {
    py::dict value;
    value["id"] = d.id;
    value["name"] = d.name;
    value["backend"] = d.backend;
    value["display_device"] = d.display_device;
    value["denoisers"] = py::cast(d.denoisers);
    value["physical_id"] = d.physical_id;
    result.append(value);
  }
  return result;
}

py::capsule create(py::handle device, py::handle width_arg, py::handle height_arg, py::handle samples_arg,
                   py::handle interactive, py::handle diagnostics, py::handle texture_cache,
                   py::handle auto_texture_cache, py::handle denoising, py::handle capture_only, py::handle threads_arg,
                   py::handle passes, py::handle progressive_display, py::handle use_tiling, py::handle tile_size,
                   py::handle hair_subdivisions, py::handle refit_meshes) {
  const int width = argument_integer<int>(width_arg);
  const int height = argument_integer<int>(height_arg);
  const int samples = argument_integer<int>(samples_arg);
  const int threads = argument_integer<int>(threads_arg);
  auto h = std::make_unique<Handle>();
  h->width = width;
  h->height = height;
  ch::SessionConfig config;
  if (py::isinstance<py::str>(device))
    config.device_id = string(device);
  else {
    try {
      for (auto id : py::reinterpret_borrow<py::iterable>(device))
        config.device_ids.push_back(string(id));
    } catch (py::error_already_set &) {
      throw std::invalid_argument("Render devices must be an ID or sequence of IDs");
    }
    if (config.device_ids.empty())
      throw std::invalid_argument("Select at least one render device");
  }
  config.width = width;
  config.height = height;
  config.samples = samples;
  config.interactive = truth(interactive);
  config.progressive_display = truth(progressive_display);
  config.use_tiling = !config.interactive && truth(use_tiling);
  config.tile_size = argument_integer<int>(tile_size);
  if (config.tile_size < 1)
    throw std::invalid_argument("Render tile size must be positive");
  config.collect_diagnostics = truth(diagnostics);
  config.use_texture_cache = truth(texture_cache);
  config.auto_texture_cache = truth(auto_texture_cache);
  config.hair_subdivisions = argument_integer<int>(hair_subdivisions);
  if (config.hair_subdivisions < 0 || config.hair_subdivisions > 24)
    throw std::invalid_argument("Curve Subdivisions must be 0 to 24");
  config.refit_meshes = config.interactive && truth(refit_meshes);
  if (threads < 0)
    throw std::invalid_argument("Render threads must be zero (automatic) or a positive count");
  config.threads = threads;
  config.texture_input_policy = ch::native_texture_input_policy();
  config.passes = {"combined", "depth", "normal", "uv", "position"};
  if (!passes.is_none()) {
    if (py::isinstance<py::str>(passes))
      throw std::invalid_argument("Pass names must be a sequence of strings");
    config.passes.clear();
    try {
      for (auto name : py::reinterpret_borrow<py::iterable>(passes))
        config.passes.push_back(string(name));
    } catch (py::error_already_set &) {
      throw std::invalid_argument("Pass names must be a sequence of strings");
    }
  }
  ch::validate_passes(config.passes);
  if (!denoising.is_none())
    config.denoising = denoise_settings(denoising);
  h->policy_slot = std::make_shared<PolicySlot>();
  h->policy_slot->replace(std::make_shared<const ch::OwnedTexturePolicy>());
  const auto image_slot = h->policy_slot;
  config.texture_input_policy.resolve_image = [image_slot](const std::string &source, double time) {
    return ch::snapshot_texture_policy(image_slot->get()).resolve_image(source, time);
  };
  h->config = config;
  h->capture_only = truth(capture_only);
  if (h->capture_only)
    h->texture_resolver = std::make_unique<ch::SnapshotTextureResolver>(config.texture_input_policy);
  else {
    ReleaseGIL release;
    h->session = std::make_unique<ch::RenderSession>(config);
  }
  py::capsule result(h.get(), "cycles_houdini.RenderSession", destroy_owned<Handle>);
  h.release();
  return result;
}

py::object fixture(py::handle capsule) {
  auto &h = handle(capsule);
  ApplyGuard guard{h};
  if (h.capture_only) {
    auto owned = std::make_shared<ch::OwnedSceneSnapshot>();
    {
      ReleaseGIL release;
      owned->state = ch::snapshot_state({}, ch::calibration_fixture());
      owned->textures = std::make_shared<const ch::OwnedTexturePolicy>();
      owned->color = ch::current_color_configuration();
      owned->portable_color = ch::snapshot_color_policy(owned->color);
    }
    auto pointer = std::make_unique<SnapshotPtr>(std::move(owned));
    py::capsule result(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
    h.desired = (*pointer)->state;
    pointer.release();
    return result;
  }
  std::uint64_t generation;
  {
    ReleaseGIL release;
    generation = h.session->apply(ch::calibration_fixture());
  }
  return py::int_(generation);
}

ch::MaterialPreviewRequest thumbnail_request(py::handle object) {
  const auto fields = sequence(object);
  if (fields.size() != 3)
    throw std::invalid_argument("A thumbnail token has three fields");
  ch::MaterialPreviewRequest result{integer<long long>(fields[0], "Invalid thumbnail request token"),
                                    integer<unsigned long long>(fields[1], "Invalid thumbnail request token"),
                                    integer<int>(fields[2], "Invalid thumbnail request token")};
  if (result.size < 32 || result.size > 256)
    throw std::invalid_argument("Invalid thumbnail request token");
  return result;
}

py::tuple thumbnail_tuple(const ch::MaterialPreviewRequest &value) {
  return py::make_tuple(value.node, value.revision, value.size);
}

py::tuple request_thumbnail(py::handle node_arg, py::handle size_arg) {
  const auto node = argument_integer<long long>(node_arg);
  const auto size = argument_integer<int>(size_arg);
  return thumbnail_tuple(ch::request_material_thumbnail(node, size));
}

py::list take_thumbnails() {
  py::list result;
  for (const auto &request : ch::take_material_previews())
    result.append(thumbnail_tuple(request));
  return result;
}

bool current_thumbnail(py::handle token) { return ch::material_preview_current(thumbnail_request(token)); }

bool publish_thumbnail(py::handle token, py::handle pixels, py::str error_arg) {
  const auto error = py::cast<std::string>(error_arg);
  c_string_checked(error);
  ReadBuffer buffer;
  buffer.request(pixels);
  return ch::publish_material_thumbnail(
      thumbnail_request(token),
      {static_cast<const std::byte *>(buffer.value.ptr), std::size_t(buffer.value.size * buffer.value.itemsize)},
      error);
}

py::dict thumbnail_stats() {
  const auto value = ch::material_preview_stats();
  py::dict result;
  result["queued"] = value.queued;
  result["coalesced"] = value.coalesced;
  result["evicted"] = value.evicted;
  result["published"] = value.published;
  result["stale"] = value.stale;
  result["image_bytes"] = value.image_bytes;
  result["images"] = value.images;
  result["queue_limit"] = 16;
  result["image_limit"] = 32;
  result["byte_limit"] = 8 * 1024 * 1024;
  return result;
}

py::bytes thumbnail_pixels(py::handle node_arg) {
  const auto node = argument_integer<long long>(node_arg);
  const auto pixels = ch::material_thumbnail_pixels(node);
  return py::bytes(reinterpret_cast<const char *>(pixels.data()), pixels.size());
}

py::capsule thumbnail_fixture(py::handle capsule, py::str domain_arg, py::handle world_medium) {
  const auto domain = py::cast<std::string>(domain_arg);
  c_string_checked(domain);
  auto &h = handle(capsule);
  if (!h.capture_only)
    throw std::logic_error("Material previews require the persistent worker");
  ApplyGuard guard{h};
  auto owned = std::make_shared<ch::OwnedSceneSnapshot>();
  const bool medium = truth(world_medium);
  {
    ReleaseGIL release;
    owned->state = ch::snapshot_state({}, ch::material_preview_fixture(domain, medium));
    owned->textures = std::make_shared<const ch::OwnedTexturePolicy>();
    owned->color = ch::current_color_configuration();
    owned->portable_color = ch::snapshot_color_policy(owned->color);
  }
  auto pointer = std::make_unique<SnapshotPtr>(std::move(owned));
  py::capsule result(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
  h.desired = (*pointer)->state;
  pointer.release();
  return result;
}

py::object blend_mesh(py::handle record, py::handle arrays, py::handle palette, py::handle previous,
                      py::handle reuse_layout) {
  std::vector<std::string> materials;
  for (auto path : sequence(palette))
    materials.push_back(string(path));
  static std::atomic<std::uint64_t> revision{0};
  return ch::blend_mesh_capsule(ch::capture_blend_mesh(
      record, arrays, materials, ++revision, previous.is_none() ? nullptr : ch::blend_mesh_from_capsule(previous),
      truth(reuse_layout)));
}

py::object blend_curves(py::handle record, py::handle arrays, py::handle palette) {
  std::vector<std::string> materials;
  for (auto path : sequence(palette))
    materials.push_back(string(path));
  static std::atomic<std::uint64_t> revision{0};
  return ch::blend_mesh_capsule(ch::capture_blend_curves(record, arrays, materials, ++revision));
}

py::object blend_points(py::handle record, py::handle arrays, py::handle palette) {
  std::vector<std::string> materials;
  for (auto path : sequence(palette))
    materials.push_back(string(path));
  static std::atomic<std::uint64_t> revision{0};
  return ch::blend_mesh_capsule(ch::capture_blend_points(record, arrays, materials, ++revision));
}

std::size_t blend_mesh_nbytes(py::handle capsule) {
  return ch::blend_mesh_nbytes(*ch::blend_mesh_from_capsule(capsule));
}

py::dict blend_mesh_storage(py::handle capsule) {
  py::dict result;
  for (const auto &[identity, bytes] : ch::blend_mesh_storage(*ch::blend_mesh_from_capsule(capsule)))
    result[py::int_(identity)] = bytes;
  return result;
}

unsigned long long unsigned_argument_mask(py::handle value, int position) {
  if (!py::isinstance<py::int_>(value))
    throw py::type_error("argument " + std::to_string(position) + " must be int, not " +
                         py::cast<std::string>(py::type::of(value).attr("__name__")));
  const auto indexed = py::module_::import("operator").attr("index")(value);
  return indexed.attr("__and__")(py::int_(std::numeric_limits<unsigned long long>::max())).cast<unsigned long long>();
}

std::string argument_script_buffer(py::handle value) {
  if (py::isinstance<py::str>(value))
    return value.cast<std::string>();
  // CPython's borrowed s# contract rejects exporters that require a release
  // callback, even a read-only memoryview. pybind11 does not expose exporter
  // slots, so inspect only this slot; acquisition and ownership use pybind11.
  // https://docs.python.org/3.13/c-api/arg.html#strings-and-buffers
  const auto *protocol = Py_TYPE(value.ptr())->tp_as_buffer;
  if (!protocol || !protocol->bf_getbuffer || protocol->bf_releasebuffer)
    throw py::type_error("script must be a string or read-only bytes-like object");
  ReadBuffer buffer;
  buffer.request(value);
  return std::string(static_cast<const char *>(buffer.value.ptr),
                     static_cast<std::size_t>(buffer.value.size * buffer.value.itemsize));
}

std::vector<ch::GeometryRequest> geometry_requests(py::handle list) {
  std::vector<ch::GeometryRequest> requests;
  if (list)
    for (auto value : sequence(list))
      requests.push_back(geometry_request(value));
  return requests;
}

void source_diagnostic_metadata(py::dict result, const std::vector<ch::SourceDiagnostic> &issues) {
  py::list values = py::list(0);
  for (const auto &d : issues) {
    py::dict value =
        py::dict(py::arg("code") = py::str(d.code.c_str()), py::arg("source") = py::str(d.source.c_str()),
                 py::arg("resource") = py::str(d.resource.c_str()), py::arg("message") = py::str(d.message.c_str()),
                 py::arg("action") = py::str(d.action.c_str()), py::arg("severity") = py::str(d.severity.c_str()),
                 py::arg("attribute") = py::str(d.attribute.c_str()));
    values.append(value);
  }
  result["source_diagnostics"] = values;
}

// "<object path>: " for a native geometry resource ("sop:<session id>/..."), so a note
// names the object the artist set up; empty when the node no longer exists.
static std::string resource_object(const std::string &resource) {
  if (!resource.starts_with("sop:"))
    return {};
  int id = 0;
  for (auto c = resource.begin() + 4; c != resource.end() && *c >= '0' && *c <= '9'; ++c)
    id = id * 10 + (*c - '0');
  const OP_Node *node = OP_Node::lookupNode(id);
  while (node && node->getOpTypeID() != OBJ_OPTYPE_ID)
    node = node->getCreator();
  return node ? node->getFullPath().toStdString() + ": " : std::string{};
}

py::dict status_value(const ch::SessionStatus &status, const ch::ExtractionStats &stats) {
  ch::PythonStatusExtraction extraction;
  extraction.cooking_seconds = stats.cooking_seconds;
  extraction.conversion_seconds = stats.conversion_seconds;
  extraction.extractions = stats.extractions;
  extraction.cache_hits = stats.cache_hits;
  extraction.packed_instances = stats.packed_instances;
  extraction.index_instance_ids = stats.index_instance_ids;
  extraction.material_motion_cooks = stats.material_motion_cooks;
  extraction.material_motion_samples = stats.material_motion_samples;
  extraction.material_motion_cache_hits = stats.material_motion_cache_hits;
  extraction.motion_warnings = stats.motion_warnings;
  for (const auto &issue : status.source_diagnostics)
    if (issue.code == "material.mesh_volume_attribute" && !extraction.resource_objects.contains(issue.resource))
      extraction.resource_objects.emplace(issue.resource, resource_object(issue.resource));
  return ch::python_status_value(status, extraction);
}

py::dict read_status(py::handle capsule) {
  auto &h = handle(capsule);
  ch::SessionStatus status;
  {
    ReleaseGIL release;
    if (h.session)
      status = h.session->status();
  }
  ch::ExtractionStats stats;
  std::array<std::uint64_t, 3> point_counts;
  double point_seconds;
  {
    std::lock_guard lock(h.state_mutex);
    stats = h.applying ? h.published_stats : capture_stats(h);
    const auto c = h.applying ? h.published_point_instances : h.adapter.point_instances().counters();
    point_counts = {c.syntheses, c.cache_hits, c.instances};
    point_seconds = c.seconds;
  }
  auto result = status_value(status, stats);
  result["rendering"] = rendering_value(status.rendering);
  point_instance_status(result, point_counts, point_seconds);
  return result;
}

py::dict diagnostics_value(const ch::SessionDiagnostics &data) {
  py::dict phases = py::dict();
  py::dict copies = py::dict();
  for (const auto &[name, duration] : data.engine_update_seconds) {
    py::float_ value = py::float_(duration);
    phases[name.c_str()] = value;
  }
  for (const auto &[name, entry] : data.buffer_copy_requests) {
    py::dict value =
        py::dict(py::arg("calls") = py::int_(entry.calls), py::arg("requested_bytes") = py::int_(entry.requested_bytes),
                 py::arg("host_seconds") = py::float_(entry.host_seconds));
    copies[name.c_str()] = value;
  }
  py::dict result =
      py::dict(py::arg("enabled") = py::bool_(data.enabled), py::arg("generation") = py::int_(data.generation),
               py::arg("core_update_seconds") = py::float_(data.core_update_seconds),
               py::arg("cancel_join_seconds") = py::float_(data.cancel_join_seconds),
               py::arg("shader_graph_preparation_seconds") = py::float_(data.shader_graph_preparation_seconds),
               py::arg("display_updates") = py::int_(data.display_updates),
               py::arg("display_conversion_seconds") = py::float_(data.display_conversion_seconds),
               py::arg("output_deliveries") = py::int_(data.output_deliveries),
               py::arg("output_delivery_seconds") = py::float_(data.output_delivery_seconds),
               py::arg("material_builds") = py::int_(data.material_builds),
               py::arg("light_updates") = py::int_(data.light_updates),
               py::arg("background_builds") = py::int_(data.background_builds),
               py::arg("engine_update_seconds") = phases, py::arg("buffer_copy_requests") = copies);
  {
    // None until the engine has recorded a scene device update; integers throughout.
    const auto &m = data.device_update_memory;
    if (!m.available) {
      result["device_update_memory"] = py::none();
    } else {
      py::list devices;
      for (const auto &d : m.devices) {
        devices.append(
            py::dict(py::arg("id") = py::str(d.id), py::arg("physical_id") = py::str(d.physical_id),
                     py::arg("backend") = py::str(d.backend), py::arg("description") = py::str(d.description),
                     py::arg("start_bytes") = py::int_(d.start_bytes), py::arg("peak_bytes") = py::int_(d.peak_bytes),
                     py::arg("end_bytes") = py::int_(d.end_bytes),
                     py::arg("max_build_temp_bytes") = py::int_(d.max_build_temp_bytes),
                     py::arg("max_build_temp_allocation_bytes") = py::int_(d.max_build_temp_allocation_bytes),
                     py::arg("build_count") = py::int_(d.build_count),
                     py::arg("reused_temp_build_count") = py::int_(d.reused_temp_build_count),
                     py::arg("local_temp_build_count") = py::int_(d.local_temp_build_count),
                     py::arg("kept_temp_bytes_before_attributes") = py::int_(d.kept_temp_bytes_before_attributes)));
      }
      result["device_update_memory"] = py::dict(
          py::arg("update_serial") = py::int_(m.update_serial), py::arg("generation") = py::int_(m.generation),
          py::arg("outcome") = py::str(m.outcome), py::arg("scope") = py::str(m.scope), py::arg("devices") = devices);
    }
  }
  py::dict cancellation =
      py::dict(py::arg("cancel_output_deliveries") = py::int_(data.cancel_output_deliveries),
               py::arg("cancel_display_updates") = py::int_(data.cancel_display_updates),
               py::arg("cancel_output_delivery_seconds") = py::float_(data.cancel_output_delivery_seconds),
               py::arg("cancel_display_conversion_seconds") = py::float_(data.cancel_display_conversion_seconds),
               py::arg("engine_snapshot_wait_seconds") = py::float_(data.engine_snapshot_wait_seconds),
               py::arg("output_reads_skipped") = py::int_(data.output_reads_skipped),
               py::arg("display_updates_skipped") = py::int_(data.display_updates_skipped),
               py::arg("cancel_output_reads_skipped") = py::int_(data.cancel_output_reads_skipped),
               py::arg("cancel_display_updates_skipped") = py::int_(data.cancel_display_updates_skipped),
               py::arg("engine_statistics_pending") = py::bool_(data.engine_statistics_pending),
               py::arg("scene_boundary_wait_seconds") = py::float_(data.scene_boundary_wait_seconds),
               py::arg("session_cancel_calls") = py::int_(data.session_cancel_calls));
  result.attr("update")(cancellation);
  py::dict reads = py::dict();
  for (const auto &[name, entry] : data.output_plane_reads) {
    py::dict value =
        py::dict(py::arg("calls") = py::int_(entry.calls), py::arg("requested_bytes") = py::int_(entry.requested_bytes),
                 py::arg("host_seconds") = py::float_(entry.host_seconds));
    reads[name.c_str()] = value;
  }
  result["output_plane_reads"] = reads;
  py::float_ vectors = py::float_(data.volume_vector_assembly_seconds);
  result["volume_vector_assembly_seconds"] = vectors;
  return result;
}

py::object diagnostics(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  ch::SessionDiagnostics data;
  {
    ReleaseGIL release;
    data = h.session->diagnostics();
  }
  return diagnostics_value(data);
}

py::object native_object_keys(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::vector<std::string> keys;
  {
    std::lock_guard lock(h.state_mutex);
    keys = h.adapter.object_keys();
  }
  py::list result = py::list(0);
  for (const auto &key : keys) {
    py::str name = py::str(key.c_str());
    result.append(name);
  }
  return result;
}

py::object native_material_references(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::vector<ch::NativeMaterialReference> references;
  {
    std::lock_guard lock(h.state_mutex);
    references = h.material_references;
  }
  return material_reference_list(references);
}

py::object native_material_paths(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::vector<std::string> paths;
  {
    std::lock_guard lock(h.state_mutex);
    paths = h.applying && !h.capture_retired ? h.accepted_material_paths : h.adapter.material_paths();
  }
  py::list result = py::list(0);
  for (const auto &path : paths) {
    py::str name = py::str(path.c_str());
    result.append(name);
  }
  return result;
}

py::list string_list(const std::vector<std::string> &values) {
  py::list result = py::list(0);
  for (const auto &value : values) {
    py::str text = py::str(value.c_str());
    result.append(text);
  }
  return result;
}

py::object native_stylesheet_resources(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::vector<std::string> resources;
  {
    std::lock_guard lock(h.state_mutex);
    resources = h.applying && !h.capture_retired ? h.accepted_stylesheet_resources : h.adapter.stylesheet_resources();
  }
  return string_list(resources);
}

py::object stylesheet_source_signature(py::handle input_arg) {
  py::handle input;
  input = input_arg;
  std::vector<std::string> resources;
  for (auto resource : sequence(input))
    resources.push_back(string(resource));
  return string_list(ch::stylesheet_source_signature(resources));
}

py::object object_stylesheet(py::str path_arg, py::handle input_arg, py::str own_arg, py::str tags_arg,
                             py::handle time_arg) {
  const char *path, *own, *tags;
  py::handle input;
  double time = CHgetEvalTime();
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  input = input_arg;
  const auto own_storage = own_arg.cast<std::string>();
  own = c_string_checked(own_storage);
  const auto tags_storage = tags_arg.cast<std::string>();
  tags = c_string_checked(tags_storage);
  if (!is_omitted(time_arg))
    time = argument_real<double>(time_arg);
  std::vector<std::string> layers;
  for (auto layer : sequence(input))
    layers.push_back(string(layer));
  auto value = ch::object_stylesheet(path, layers, own, tags, time);
  py::object errors = string_list(value.errors);
  py::object resources = string_list(value.resources);
  return py::dict(py::arg("json") = py::str(value.json.c_str()), py::arg("errors") = errors,
                  py::arg("resources") = resources);
}

py::object native_stats(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  ch::ExtractionStats stats;
  {
    std::lock_guard lock(h.state_mutex);
    stats = h.applying ? h.published_stats : capture_stats(h);
  }
  py::dict result = py::dict(py::arg("points") = py::int_(stats.points), py::arg("curves") = py::int_(stats.curves),
                             py::arg("index_primitive_ids") = py::int_(stats.index_primitive_ids),
                             py::arg("motion_samples") = py::int_(stats.motion_samples),
                             py::arg("motion_cooks") = py::int_(stats.motion_cooks),
                             py::arg("shutter_open_seconds") = py::float_(stats.shutter_open_seconds),
                             py::arg("shutter_close_seconds") = py::float_(stats.shutter_close_seconds),
                             py::arg("material_binding_scans") = py::int_(stats.material_binding_scans),
                             py::arg("material_binding_cache_hits") = py::int_(stats.material_binding_cache_hits),
                             py::arg("partition_scans") = py::int_(stats.partition_scans),
                             py::arg("partition_cache_hits") = py::int_(stats.partition_cache_hits),
                             py::arg("partition_cache_bytes") = py::int_(stats.partition_cache_bytes),
                             py::arg("component_checks") = py::int_(stats.component_checks),
                             py::arg("component_reuses") = py::int_(stats.component_reuses),
                             py::arg("component_check_seconds") = py::float_(stats.component_check_seconds),
                             py::arg("mesh_topology_builds") = py::int_(stats.mesh_topology_builds),
                             py::arg("mesh_topology_reuses") = py::int_(stats.mesh_topology_reuses),
                             py::arg("mesh_topology_scans") = py::int_(stats.mesh_topology_scans),
                             py::arg("mesh_triangulated_faces") = py::int_(stats.mesh_triangulated_faces),
                             py::arg("mesh_retained_faces") = py::int_(stats.mesh_retained_faces),
                             py::arg("mesh_capture_cache_bytes") = py::int_(stats.mesh_capture_cache_bytes));
  for (const auto &[name, count] : {std::pair{"mesh_named_fields_captured", stats.mesh_named_fields_captured},
                                    std::pair{"mesh_named_fields_reused", stats.mesh_named_fields_reused},
                                    std::pair{"mesh_named_values_read", stats.mesh_named_values_read},
                                    std::pair{"mesh_named_values_reused", stats.mesh_named_values_reused}}) {
    py::int_ value = py::int_(count);
    result[name] = value;
  }
  for (const auto &[name, count] : {std::pair{"dense_field_conversions", stats.dense_field_conversions},
                                    std::pair{"direct_instances_expanded", stats.direct_instances_expanded},
                                    std::pair{"direct_population_reuses", stats.direct_population_reuses},
                                    std::pair{"packed_population_reuses", stats.packed_population_reuses},
                                    std::pair{"adapter_full_object_updates", stats.adapter_full_object_updates},
                                    std::pair{"adapter_pose_updates", stats.adapter_pose_updates},
                                    std::pair{"adapter_attribute_updates", stats.adapter_attribute_updates},
                                    std::pair{"adapter_pose_attribute_updates", stats.adapter_pose_attribute_updates},
                                    std::pair{"packed_recycled_objects", stats.packed_recycled_objects},
                                    std::pair{"retained_dense_voxel_bytes", stats.retained_dense_voxel_bytes}}) {
    py::int_ value = py::int_(count);
    result[name] = value;
  }
  result["packed_recycle_fallback"] = py::str(stats.packed_recycle_fallback);
  for (const auto &[name, seconds] : {std::pair{"cooking_seconds", stats.cooking_seconds},
                                      std::pair{"geometry_conversion_seconds", stats.conversion_seconds}}) {
    py::float_ value = py::float_(seconds);
    result[name] = value;
  }
  py::dict phases = py::dict();
  for (const auto &[name, seconds] : stats.packed_assembly_seconds) {
    py::float_ value = py::float_(seconds);
    phases[name.c_str()] = value;
  }
  py::dict packed =
      py::dict(py::arg("template_components") = py::int_(stats.packed_template_components),
               py::arg("expanded_components") = py::int_(stats.packed_expanded_components),
               py::arg("final_components") = py::int_(stats.packed_final_components),
               py::arg("deformation_tasks") = py::int_(stats.packed_deformation_tasks),
               py::arg("profile_groups") = py::int_(stats.packed_profile_groups), py::arg("seconds") = phases);
  result["packed_assembly"] = packed;
  py::dict fragments = py::dict(); // Process-wide, cumulative (packed_fragments.h).
  for (const auto &[name, value] : ch::packed_fragment_stats()) {
    py::float_ number = py::float_(value);
    fragments[name] = number;
  }
  result["packed_fragments"] = fragments;
  const auto &life = stats.lifecycle;
  py::dict lifecycle = py::dict(py::arg("prepare_seconds") = py::float_(life.prepare_seconds),
                                py::arg("publication_seconds") = py::float_(life.publication_seconds),
                                py::arg("retirement_seconds") = py::float_(life.retirement_seconds),
                                py::arg("publications") = py::int_(life.publications),
                                py::arg("retirements") = py::int_(life.retirements),
                                py::arg("retired_scenes") = py::int_(life.retired_scenes),
                                py::arg("retired_objects") = py::int_(life.retired_objects),
                                py::arg("retired_geometry") = py::int_(life.retired_geometry),
                                py::arg("last_retired_objects") = py::int_(life.last_retired_objects),
                                py::arg("retired_snapshots") = py::int_(life.retired_snapshots),
                                py::arg("retired_snapshot_objects") = py::int_(life.retired_snapshot_objects),
                                py::arg("retirement_on_host") = py::bool_(life.retirement_on_host));
  result["scene_lifecycle"] = lifecycle;
  motion_warning_metadata(result, stats);
  return result;
}

py::object scene_definition_fallback(py::str path_arg, py::handle script_arg, py::handle inputs_arg,
                                     py::handle outputs_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene definitions require Houdini's host thread");
  const char *path{};
  const char *script{};
  Py_ssize_t bytes{}, inputs{}, outputs{};
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  const auto script_storage = argument_script_buffer(script_arg);
  script = script_storage.data();
  bytes = static_cast<Py_ssize_t>(script_storage.size());
  inputs = argument_integer<Py_ssize_t>(inputs_arg);
  outputs = argument_integer<Py_ssize_t>(outputs_arg);
  if (inputs < 0 || outputs < 0 || inputs > std::numeric_limits<int>::max() ||
      outputs > std::numeric_limits<int>::max())
    throw std::invalid_argument("Invalid scene definition connector count");
  auto *node = OPgetDirector()->findNode(path);
  if (!node)
    throw std::invalid_argument("Scene fallback node does not exist");
  if (!ch::is_native_shader_operator(*node->getOperator()))
    return py::dict(py::arg("registered") = py::bool_(false), py::arg("unowned") = py::bool_(true));
  const auto changed = ch::register_scene_definition(*node, std::string(script, static_cast<std::size_t>(bytes)),
                                                     static_cast<unsigned>(inputs), static_cast<unsigned>(outputs));
  return py::dict(py::arg("registered") = py::bool_(true), py::arg("updated") = py::bool_(changed));
}

py::object clear_scene_definitions() {
  ch::clear_scene_definitions();
  return py::none();
}

py::object cancel(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  {
    ReleaseGIL release;
    h.session->cancel();
  }
  return py::none();
}

py::object retire_capture_impl(py::handle capsule_arg, py::handle entered_arg, py::handle release_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  {
    std::lock_guard lock(h.state_mutex);
    if (!h.capture_only)
      throw std::logic_error("Owned capture retirement requires a capture-only handle");
    if (h.applying)
      throw std::logic_error("Cannot retire capture resources inside a scene transaction");
  }
  ApplyGuard guard{h};
  if (entered_arg) {
    entered_arg.attr("set")();
    if (!py::cast<bool>(release_arg.attr("wait")(10.0)))
      throw std::runtime_error("Timed out waiting to retire native capture");
  }
  {
    std::lock_guard lock(h.state_mutex);
    h.capture_retired = true;
  }
  const auto started = std::chrono::steady_clock::now();
  const auto objects = h.desired ? h.desired->objects->size() : 0;
  {
    ReleaseGIL release;
    // These owners contain frozen resources, not live node evaluation or
    // display callbacks. Keep final display/host-handle release in close().
    std::lock_guard lock(h.state_mutex);
    h.desired.reset();
    h.retired_snapshot.reset();
    h.texture_state.reset();
    h.cop_images.clear();
    h.adapter.reset();
    h.texture_resolver.reset();
    h.native_material_keys.clear();
    h.material_references.clear();
    h.accepted_material_paths.clear();
    h.accepted_stylesheet_resources.clear();
    h.captured_source_diagnostics.clear();
  }
  {
    std::lock_guard lock(h.state_mutex);
    h.published_stats = capture_stats(h);
  }
  return py::dict(py::arg("seconds") =
                      py::float_(std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()),
                  py::arg("on_host_thread") = py::bool_(UT_Thread::isMainThread()),
                  py::arg("snapshot_objects") = py::int_(static_cast<unsigned long long>(objects)));
}

py::object retire_capture(py::handle capsule_arg) { return retire_capture_impl(capsule_arg, {}, {}); }

py::object test_retire_capture_rendezvous(py::handle capsule_arg, py::handle entered_arg, py::handle release_arg) {
  return retire_capture_impl(capsule_arg, entered_arg, release_arg);
}

py::object close_session(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::shared_ptr<ch::TileDisplay> display;
  {
    std::unique_lock lock(h.state_mutex);
    while (h.closing) {
      if (h.closing_thread == std::this_thread::get_id())
        throw std::logic_error("Native session close cannot be reentered");
      lock.unlock();
      {
        ReleaseGIL release;
        std::unique_lock wait_lock(h.state_mutex);
        h.state_changed.wait(wait_lock, [&] { return !h.closing; });
      }
      lock.lock();
    }
    if (h.applying)
      throw std::logic_error("Cannot close a session inside a scene transaction");
    if (h.closed)
      return py::none();
    h.closing = true;
    h.closing_thread = std::this_thread::get_id();
    display.swap(h.display);
  }
  try {
    {
      ReleaseGIL release;
      display.reset();
      if (h.session)
        h.session->close();
      {
        std::lock_guard lock(h.state_mutex);
        h.desired.reset();
        h.retired_snapshot.reset();
        h.texture_state.reset();
        h.cop_images.clear();
        if (h.policy_slot)
          h.policy_slot->replace(std::make_shared<const ch::OwnedTexturePolicy>());
      }
    }
    {
      std::lock_guard lock(h.state_mutex);
      h.adapter.reset();
      h.native_material_keys.clear();
      h.material_references.clear();
      h.accepted_material_paths.clear();
      h.accepted_stylesheet_resources.clear();
      h.closed = true;
      h.closing = false;
      h.closing_thread = {};
    }
  } catch (...) {
    {
      std::lock_guard lock(h.state_mutex);
      h.closing = false;
      h.closing_thread = {};
    }
    h.state_changed.notify_all();
    throw;
  }
  h.state_changed.notify_all();
  return py::none();
}

py::object pause(py::handle capsule_arg, py::handle paused_arg) {
  py::handle capsule;
  int paused;
  capsule = capsule_arg;
  paused = truth(paused_arg);
  auto &h = handle(capsule);
  {
    ReleaseGIL release;
    h.session->pause(paused != 0);
  }
  return py::none();
}

py::object color_policy() {
  ch::ColorPolicy policy;
  {
    ReleaseGIL release;
    policy = ch::color_policy();
  }
  py::dict views = py::dict();
  py::dict defaults = py::dict();
  py::list spaces = py::list(0);
  for (const auto &name : policy.output_spaces) {
    py::str v = py::str(name.c_str());
    spaces.append(v);
  }
  for (const auto &[display, list] : policy.views) {
    py::list values = py::list(0);
    for (const auto &name : list) {
      py::str v = py::str(name.c_str());
      values.append(v);
    }
    views[display.c_str()] = values;
    py::str name = py::str(policy.default_views.at(display).c_str());
    defaults[display.c_str()] = name;
  }
  return py::dict(py::arg("working_space") = py::str(policy.working_space.c_str()),
                  py::arg("default_display") = py::str(policy.default_display.c_str()), py::arg("views") = views,
                  py::arg("default_views") = defaults,
                  py::arg("configuration_id") = py::str(policy.configuration_id.c_str()),
                  py::arg("config_path") = py::str(ch::color_configuration_path(*ch::current_color_configuration())),
                  py::arg("render_policy_id") = py::str(policy.render_policy_id.c_str()),
                  py::arg("output_interop_id") = py::str(policy.output_interop_id.c_str()),
                  py::arg("output_interop_selection") = py::str(policy.output_interop_selection.c_str()),
                  py::arg("builtin_configuration") = py::str(policy.builtin_configuration.c_str()),
                  py::arg("output_spaces") = spaces);
}

py::object blackbody_temperature_color(py::handle kelvin_arg) {
  double kelvin;
  kelvin = argument_real<double>(kelvin_arg);
  ch::Float3 rgb;
  {
    ReleaseGIL release;
    rgb = ch::blackbody_temperature_color(float(kelvin));
  }
  return py::make_tuple(py::float_(rgb.x), py::float_(rgb.y), py::float_(rgb.z));
}

py::object synchronize_color() {
  bool changed;
  {
    ReleaseGIL release;
    changed = ch::synchronize_color_configuration();
  }
  return py::bool_(changed);
}

py::list texture_cache_plan(py::handle snapshot_arg) {
  const auto owned = snapshot(snapshot_arg);
  std::vector<ch::TextureCachePlanItem> items;
  {
    ReleaseGIL release;
    items = ch::texture_cache_plan(*owned);
  }
  py::list result;
  std::set<std::pair<std::string, std::string>> seen;
  for (auto &item : items) {
    if (!seen.emplace(item.source, item.destination).second)
      continue;
    auto pointer = std::make_unique<ch::TextureCachePlanItem>(std::move(item));
    py::dict entry(py::arg("source") = pointer->source, py::arg("destination") = pointer->destination);
    py::capsule capsule(pointer.get(), "cycles_houdini.TextureCacheItem", destroy_owned<ch::TextureCachePlanItem>);
    pointer.release();
    entry["handle"] = capsule;
    result.append(entry);
  }
  return result;
}
void texture_cache_generate(py::handle value) {
  const auto capsule = py::cast<py::capsule>(value);
  if (std::string(capsule.name() ? capsule.name() : "") != "cycles_houdini.TextureCacheItem")
    throw std::invalid_argument("Expected an owned texture cache plan item");
  const auto *item = static_cast<const ch::TextureCachePlanItem *>(capsule.get_pointer());
  ReleaseGIL release;
  if (!item)
    throw std::invalid_argument("Invalid owned texture cache plan");
  ch::generate_texture_cache(*item, ch::native_texture_input_policy());
}

py::object texture_color_info(py::str filename_arg, py::handle read_as_is_arg, py::handle observe_texture_cache_arg) {
  const char *filename;
  int read_as_is = 1, observe_texture_cache = 0;
  const auto filename_storage = filename_arg.cast<std::string>();
  filename = c_string_checked(filename_storage);
  read_as_is = truth(read_as_is_arg);
  observe_texture_cache = truth(observe_texture_cache_arg);
  ch::NativeTextureColorInfo info;
  {
    ReleaseGIL release;
    info = ch::native_texture_color_info(filename, read_as_is != 0, observe_texture_cache != 0);
  }
  return py::dict(py::arg("filename_space") = py::str(info.filename_space.c_str()),
                  py::arg("plane_space") = py::str(info.plane_space.c_str()),
                  py::arg("native_space") = py::str(info.native_space.c_str()),
                  py::arg("guessed_space") = py::str(info.guessed_space.c_str()),
                  py::arg("heuristic_space") = py::str(info.heuristic_space.c_str()),
                  py::arg("storage") = py::str(info.storage.c_str()),
                  py::arg("color_model") = py::str(info.color_model.c_str()),
                  py::arg("plane_name") = py::str(info.plane_name.c_str()), py::arg("planes") = py::int_(info.planes),
                  py::arg("texture_space") = py::str(info.texture_space.c_str()));
}

py::object cop_image_source_info(py::str source_arg) {
  const char *source;
  const auto source_storage = source_arg.cast<std::string>();
  source = c_string_checked(source_storage);
  ch::NativeCopSource info;
  {
    ReleaseGIL release;
    info = ch::cop_image_source(source);
  }
  py::list dependencies = py::list(info.dependencies.size());
  for (std::size_t n = 0; n < info.dependencies.size(); ++n) {
    dependencies[n] = py::int_(info.dependencies[n]);
  }
  py::list nodes = py::list(info.node_ids.size());
  for (std::size_t n = 0; n < info.node_ids.size(); ++n)
    nodes[n] = py::int_(info.node_ids[n]);
  return py::dict(py::arg("node_id") = py::int_(info.node_id), py::arg("dependencies") = dependencies,
                  py::arg("nodes") = nodes, py::arg("pinned_frame") = py::bool_(info.pinned_frame));
}

py::object cop_capture_stats(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::shared_ptr<const ch::NativeCopBatch> batch;
  std::uint64_t acquisitions, cache_hits;
  double acquisition_seconds, conversion_seconds;
  {
    std::lock_guard lock(h.state_mutex);
    batch = h.cop_images.latest();
    acquisitions = h.cop_images.acquisitions;
    cache_hits = h.cop_images.cache_hits;
    acquisition_seconds = h.cop_images.acquisition_seconds;
    conversion_seconds = h.cop_images.conversion_seconds;
  }
  std::uint64_t bytes{};
  if (batch)
    for (const auto &[key, image] : batch->images)
      bytes += image->pixels.size() * sizeof(float);
  return py::dict(py::arg("captures") = py::int_(acquisitions), py::arg("cache_hits") = py::int_(cache_hits),
                  py::arg("resources") = py::int_(batch ? batch->images.size() : 0),
                  py::arg("pixel_bytes") = py::int_(bytes),
                  py::arg("acquisition_seconds") = py::float_(acquisition_seconds),
                  py::arg("conversion_seconds") = py::float_(conversion_seconds));
}

py::object cop_image_sources(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::shared_ptr<const ch::NativeCopBatch> batch;
  {
    std::lock_guard lock(h.state_mutex);
    batch = h.cop_images.latest();
  }
  std::set<std::string> sources;
  if (!batch)
    return py::none();
  if (batch)
    for (const auto &[key, entry] : batch->entries)
      sources.insert(key.source);
  py::list result = py::list(0);
  for (const auto &source : sources) {
    py::str text = py::str(source.c_str());
    result.append(text);
  }
  return result;
}

CopImagePtr cop_image(py::handle value) {
  const auto *pointer = capsule_pointer<CopImagePtr>(value, "cycles_houdini.CopImage");
  if (!pointer)
    throw std::invalid_argument("Invalid owned COP image");
  return *pointer;
}

py::object freeze_cop_image(py::str source_arg, py::handle time_arg) {
  const char *source;
  double time;
  const auto source_storage = source_arg.cast<std::string>();
  source = c_string_checked(source_storage);
  time = argument_real<double>(time_arg);
  CopImagePtr image;
  {
    ReleaseGIL release;
    image = ch::freeze_cop_image(source, time);
  }
  auto pointer = std::make_unique<CopImagePtr>(std::move(image));
  auto value = py::capsule(pointer.get(), "cycles_houdini.CopImage", destroy_owned<CopImagePtr>);
  if (value)
    pointer.release();
  return value;
}

py::object cop_image_info(py::handle value_arg) {
  py::handle value;
  value = value_arg;
  const auto image = cop_image(value);
  return py::dict(py::arg("source") = py::str(image->source.c_str()),
                  py::arg("colorspace") = py::str(image->colorspace.c_str()),
                  py::arg("storage") = py::str(image->storage.c_str()), py::arg("node_id") = py::int_(image->node_id),
                  py::arg("width") = py::int_(image->width), py::arg("height") = py::int_(image->height),
                  py::arg("channels") = py::int_(image->channels), py::arg("rotation") = py::int_(image->rotation),
                  py::arg("source_version") = py::int_(std::uint64_t(image->source_version)),
                  py::arg("requested_time") = py::float_(image->requested_time),
                  py::arg("source_time") = py::float_(image->source_time));
}

py::object cop_image_pixels(py::handle value_arg) {
  py::handle value;
  value = value_arg;
  const auto image = cop_image(value);
  std::vector<float> pixels;
  {
    ReleaseGIL release;
    pixels = ch::cop_image_pixels(*image);
  }
  const auto size = Py_ssize_t(pixels.size() * sizeof(float));
  auto result = py::bytes(static_cast<const char *>(nullptr), size);
  auto *target = unpublished_bytes_data(result);
  {
    ReleaseGIL release;
    if (size)
      std::memcpy(target, pixels.data(), size);
    std::vector<float>().swap(pixels);
  }
  return result;
}

py::object resolve_camera_motion(py::handle nominal_arg, py::handle sample_values_arg, py::handle shutter_arg,
                                 py::str source_arg, py::handle vectors_arg, py::handle width_arg,
                                 py::handle height_arg, py::handle pixel_aspect_arg) {
  py::handle nominal, sample_values;
  double shutter;
  const char *source;
  int vectors = 0, width = 1, height = 1;
  double pixel_aspect = 1;
  nominal = nominal_arg;
  sample_values = sample_values_arg;
  shutter = argument_real<double>(shutter_arg);
  const auto source_storage = source_arg.cast<std::string>();
  source = c_string_checked(source_storage);
  vectors = truth(vectors_arg);
  width = argument_integer<int>(width_arg);
  height = argument_integer<int>(height_arg);
  pixel_aspect = argument_real<double>(pixel_aspect_arg);
  const auto parse = [](py::handle value) {
    ch::CameraSourceSample result;
    result.projection = world_integer(item(value, "projection_code"));
    auto &c = result.value;
    c.transform = matrix(item(value, "transform"));
    c.short_axis_fov = float(number(item(value, "fov")));
    c.near_clip = float(number(item(value, "near")));
    c.far_clip = float(number(item(value, "far")));
    c.ortho_scale = float(number(item(value, "ortho_scale")));
    if (auto p = item(value, "panorama", false); p && !p.is_none())
      c.panorama = camera_panorama(p);
    return result;
  };
  const auto count = py::len(sample_values);
  if (count > 129)
    throw std::invalid_argument("Too many camera source samples");
  std::vector<ch::CameraSourceSample> samples;
  for (auto value : sequence(sample_values))
    samples.push_back(parse(value));
  const auto c = ch::resolve_camera_source(parse(nominal), samples, source, float(shutter), vectors != 0, width, height,
                                           float(pixel_aspect));
  py::object transform = camera_matrix_value(c.transform);
  py::list motion = py::list(0);
  for (const auto &sample : c.motion) {
    py::object value = camera_matrix_value(sample);
    motion.append(value);
  }
  py::dict result = py::dict(
      py::arg("transform") = transform, py::arg("fov") = py::float_(double(c.short_axis_fov)),
      py::arg("near") = py::float_(double(c.near_clip)), py::arg("far") = py::float_(double(c.far_clip)),
      py::arg("orthographic") = py::bool_(c.orthographic), py::arg("ortho_scale") = py::float_(double(c.ortho_scale)),
      py::arg("shutter_frames") = py::float_(double(c.shutter_frames)), py::arg("motion") = motion);
  source_diagnostic_metadata(result, c.source_diagnostics);
  py::str source_path = py::str(source);
  result["source_path"] = source_path;
  const auto add_panorama = [](py::dict target, const ch::PanoramaSettings &p) {
    py::dict panorama =
        py::dict(py::arg("type") = py::str(p.type.c_str()), py::arg("fisheye_fov") = py::float_(double(p.fisheye_fov)),
                 py::arg("fisheye_lens") = py::float_(double(p.fisheye_lens)),
                 py::arg("latitude_min") = py::float_(double(p.latitude_min)),
                 py::arg("latitude_max") = py::float_(double(p.latitude_max)),
                 py::arg("longitude_min") = py::float_(double(p.longitude_min)),
                 py::arg("longitude_max") = py::float_(double(p.longitude_max)),
                 py::arg("cylindrical_u_min") = py::float_(double(p.cylindrical_u_min)),
                 py::arg("cylindrical_u_max") = py::float_(double(p.cylindrical_u_max)),
                 py::arg("cylindrical_v_min") = py::float_(double(p.cylindrical_v_min)),
                 py::arg("cylindrical_v_max") = py::float_(double(p.cylindrical_v_max)),
                 py::arg("sensor_width") = py::float_(double(p.sensor_width)),
                 py::arg("sensor_height") = py::float_(double(p.sensor_height)),
                 py::arg("sensor_fit") = py::str(p.sensor_fit.c_str()));
    py::list coefficients = py::list(py::make_tuple(
        py::float_(double(p.polynomial[0])), py::float_(double(p.polynomial[1])), py::float_(double(p.polynomial[2])),
        py::float_(double(p.polynomial[3])), py::float_(double(p.polynomial[4]))));
    panorama["polynomial"] = coefficients;
    target["panorama"] = panorama;
  };
  if (c.panorama)
    add_panorama(result, *c.panorama);
  py::list intrinsics = py::list(0);
  for (const auto &sample : c.intrinsic_motion) {
    py::dict value = py::dict(py::arg("fov") = py::float_(double(sample.short_axis_fov)),
                              py::arg("near") = py::float_(double(sample.near_clip)),
                              py::arg("far") = py::float_(double(sample.far_clip)),
                              py::arg("ortho_scale") = py::float_(double(sample.ortho_scale)));
    if (sample.panorama)
      add_panorama(value, *sample.panorama);
    intrinsics.append(value);
  }
  result["intrinsic_motion"] = intrinsics;
  return result;
}

py::object process_memory_json() {
  std::string data;
  {
    ReleaseGIL release;
    data = ch::process_memory_json();
  }
  return py::str(data.data(), data.size());
}

py::object shader_catalogue_json() {
  std::string data;
  {
    ReleaseGIL release;
    data = ch::shader_catalogue_json();
  }
  return py::str(data.data(), data.size());
}

py::object shader_curve_table(py::handle source_arg, py::str kind_arg) {
  py::handle source;
  const char *kind;
  source = source_arg;
  const auto kind_storage = kind_arg.cast<std::string>();
  kind = c_string_checked(kind_storage);
  const std::string name(kind);
  if (name != "float" && name != "rgb" && name != "vector")
    throw std::invalid_argument("Unknown shader curve kind");
  const auto controls = std::get<std::vector<int>>(shader_value(source, false, true, 780));
  std::string data;
  {
    ReleaseGIL release;
    const auto table = ch::prepare_shader_curve(controls, name == "float" ? ch::ShaderCurveKind::Float
                                                          : name == "rgb" ? ch::ShaderCurveKind::RGB
                                                                          : ch::ShaderCurveKind::Vector);
    std::ostringstream out;
    out << std::setprecision(std::numeric_limits<float>::max_digits10) << "{\"minimum\":" << table.minimum
        << ",\"maximum\":" << table.maximum << ",\"extrapolate\":" << (table.extrapolate ? "true" : "false")
        << ",\"table\":[";
    bool comma = false;
    for (const auto &row : table.values) {
      if (comma)
        out << ',';
      comma = true;
      out << '[' << row[0] << ',' << row[1] << ',' << row[2] << ']';
    }
    out << "]}";
    data = out.str();
  }
  return py::str(data.data(), data.size());
}

py::object ramp_preview_sample(py::handle capsule_arg, py::handle factor_arg) {
  py::handle capsule;
  float factor;
  capsule = capsule_arg;
  factor = argument_real<float>(factor_arg);
  auto *data = capsule_pointer<RampPreview>(capsule, "cycles_houdini.RampPreview");
  if (!data)
    throw std::invalid_argument("Invalid immutable ramp preview");
  if (!std::isfinite(factor))
    throw std::invalid_argument("Ramp preview factor must be finite");
  const auto v = data->sample(factor);
  return py::make_tuple(py::float_(v[0]), py::float_(v[1]), py::float_(v[2]), py::float_(v[3]));
}

py::object ramp_preview(py::handle color_arg, py::handle alpha_arg, py::handle factors_arg, py::handle options_arg) {
  py::handle color, alpha, factors, options;
  color = color_arg;
  alpha = alpha_arg;
  factors = factors_arg;
  options = options_arg;
  const auto colors = std::get<std::vector<int>>(shader_value(color, false, true, 780));
  const auto alphas = std::get<std::vector<int>>(shader_value(alpha, false, true, 780));
  if (colors.size() < 5 || alphas.size() < 5 || colors[4] != 1 || alphas[4] != 0)
    throw std::invalid_argument("Ramp preview requires owned Color and scalar Alpha sources");
  if ((!py::isinstance<py::list>(factors) && !py::isinstance<py::tuple>(factors)) || py::len(factors) < 1 ||
      py::len(factors) > 4096)
    throw std::invalid_argument("Ramp preview requires 1..4096 factors");
  std::vector<float> positions;
  for (auto p : sequence(factors)) {
    const auto v = float(number(p));
    if (!std::isfinite(v))
      throw std::invalid_argument("Non-finite ramp preview factor");
    positions.push_back(v);
  }
  auto settings = read_display_settings(options);
  settings.pass = "combined";
  settings.channels = "rgb";
  auto prepared = std::make_unique<RampPreview>();
  std::vector<float> raw;
  ch::DisplayImage display;
  {
    ReleaseGIL release;
    try {
      prepared->color = ch::prepare_owned_ramp(colors);
    } catch (const ch::RampSourceError &e) {
      prepared->color_issue = e.what();
    }
    try {
      prepared->alpha = ch::prepare_owned_ramp(alphas);
    } catch (const ch::RampSourceError &e) {
      prepared->alpha_issue = e.what();
    }
    ch::FrameEvent frame;
    frame.width = frame.full_width = int(positions.size());
    frame.height = frame.full_height = 1;
    frame.color_configuration = ch::current_color_configuration();
    for (const auto &pass : ch::pass_catalogue())
      if (pass.id == "combined")
        frame.requested_passes.push_back(pass);
    auto &plane = frame.planes.emplace_back();
    plane.pass = "combined";
    plane.channels = 4;
    for (float position : positions) {
      const auto v = prepared->sample(position);
      raw.insert(raw.end(), v.begin(), v.end());
      plane.pixels.insert(plane.pixels.end(), {v[0], v[1], v[2], 1.f});
    }
    display = ch::display_image(frame, settings);
  }
  py::bytes linear = py::bytes(reinterpret_cast<const char *>(raw.data()), Py_ssize_t(raw.size() * sizeof(float)));
  py::bytes rgba = py::bytes(reinterpret_cast<const char *>(display.rgba.data()), Py_ssize_t(display.rgba.size()));
  py::capsule capsule = py::capsule(prepared.get(), "cycles_houdini.RampPreview", destroy_owned<RampPreview>);
  prepared.release();
  return py::dict(py::arg("linear") = linear, py::arg("rgba") = rgba, py::arg("prepared") = capsule,
                  py::arg("color_issue") =
                      py::str(capsule_pointer<RampPreview>(capsule, "cycles_houdini.RampPreview")->color_issue.c_str()),
                  py::arg("alpha_issue") = py::str(
                      capsule_pointer<RampPreview>(capsule, "cycles_houdini.RampPreview")->alpha_issue.c_str()));
}

py::object vop_input_activity(py::handle id_arg, py::handle time_arg, py::handle values_arg) {
  int id{};
  double time{};
  py::handle values = py::none();
  id = argument_integer<int>(id_arg);
  time = argument_real<double>(time_arg);
  if (!is_omitted(values_arg))
    values = values_arg;
  if (!UT_Thread::isMainThread())
    throw std::logic_error("VOP input inspection requires Houdini's host thread");
  if (!std::isfinite(time))
    throw std::invalid_argument("VOP evaluation time must be finite");
  auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  if (!node)
    throw std::invalid_argument("Missing VOP for input inspection");
  std::map<std::string, std::string> overrides;
  if (!values.is_none()) {
    if (!py::isinstance<py::dict>(values))
      throw std::invalid_argument("Input activity values require a dictionary of string tokens");
    for (auto [key, value] : py::reinterpret_borrow<py::dict>(values))
      overrides.emplace(string(key), string(value));
  }
  py::dict result = py::dict();
  for (const auto &[name, active] : ch::native_shader_input_activity(*node, time, overrides))
    result[name.c_str()] = py::bool_(active);
  return result;
}

py::object vop_input_controls(py::handle id_arg) {
  int id{};
  id = argument_integer<int>(id_arg);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("VOP input controls require Houdini's host thread");
  auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  if (!node)
    throw std::invalid_argument("Missing VOP for input controls");
  py::list result = py::list(0);
  for (const auto &name : ch::native_shader_input_controls(*node)) {
    py::str value = py::str(name.c_str());
    result.append(value);
  }
  return result;
}

py::object vop_connector_layout(py::handle id_arg) {
  int id{};
  id = argument_integer<int>(id_arg);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("VOP layout inspection requires Houdini's host thread");
  auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  if (!node)
    throw std::invalid_argument("Missing VOP for layout inspection");
  py::list result = py::list(0);
  for (const auto &connector : node->getVisibleInputConnectors()) {
    if (!connector.myIsGroup && connector.myIndex < 0)
      continue;
    UT_String name("");
    if (!connector.myIsGroup)
      node->getInputName(name, connector.myIndex);
    py::dict item = py::dict(py::arg("name") = py::str(name.c_str()), py::arg("index") = py::int_(connector.myIndex),
                             py::arg("group") = py::bool_(connector.myIsGroup),
                             py::arg("label") = py::str(connector.myGroupName.c_str()));
    result.append(item);
  }
  return result;
}

py::object vop_output_override(py::handle id_arg, py::str output_arg) {
  int id{};
  const char *output{};
  id = argument_integer<int>(id_arg);
  const auto output_storage = output_arg.cast<std::string>();
  output = c_string_checked(output_storage);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("VOP output modes require Houdini's host thread");
  auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  if (!node)
    throw std::invalid_argument("Missing VOP for output override inspection");
  const auto index = node->getOutputFromName(UT_String(output));
  if (index < 0)
    throw std::invalid_argument("Missing VOP output for override inspection");
  const bool bypass = node->getBypass(), debug = node->getDebug();
  if (!bypass && !debug)
    return py::none();
  auto *modes = node->getOutputInfoManager();
  const bool authored = modes->doesInfoExist(output);
  const auto mode = authored ? (bypass ? modes->getBypassOutputMode(output) : modes->getDebugOutputMode(output))
                             : VOP_OUTPUT_MODE_NONE;
  if (!bypass && (mode == VOP_OUTPUT_MODE_EXECUTE || mode == VOP_OUTPUT_MODE_NONE))
    return py::none();
  std::string input;
  if (authored && mode == VOP_OUTPUT_MODE_PASSTHROUGH) {
    UT_String selected;
    modes->findCorrespondingInputName(output, debug, bypass, selected);
    input = selected.toStdString();
  } else if (!authored && bypass)
    input = ch::native_shader_bypass_input(*node, output);
  if (!input.empty())
    return py::dict(py::arg("mode") = py::str("input"), py::arg("input") = py::str(input.c_str()));
  // A muted output with no pass-through produces its type's zero value,
  // matching a disconnected socket. Never invent an emissive error marker.
  auto *values = authored && mode == VOP_OUTPUT_MODE_CONSTANT && !modes->getUseDefaultValue(output)
                     ? (bypass ? modes->getBypassValuePtr(output) : modes->getDebugValuePtr(output))
                     : nullptr;
  const bool value = values && values->hasOption(VOP_OUTPUT_VALUE_NAME);
  py::object constant;
  switch (node->getOutputType(index)) {
  case VOP_TYPE_BSDF:
    constant = py::none();
    break;
  case VOP_TYPE_VECTOR: {
    const auto v = value ? values->getOptionV3(VOP_OUTPUT_VALUE_NAME) : UT_Vector3D(0, 0, 0);
    constant = py::make_tuple(py::float_(v.x()), py::float_(v.y()), py::float_(v.z()));
    break;
  }
  case VOP_TYPE_FLOAT:
    constant = py::float_(value ? values->getOptionF(VOP_OUTPUT_VALUE_NAME) : 0.);
    break;
  case VOP_TYPE_INTEGER:
    constant = py::int_(value ? values->getOptionI(VOP_OUTPUT_VALUE_NAME) : 0);
    break;
  case VOP_TYPE_STRING:
    constant = py::str(value ? values->getOptionS(VOP_OUTPUT_VALUE_NAME).c_str() : "");
    break;
  default:
    throw std::invalid_argument("Unsupported VOP override value type");
  }
  return py::dict(py::arg("mode") = py::str("constant"), py::arg("value") = constant);
}

py::object textures_changed(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  bool changed;
  {
    ReleaseGIL release;
    changed = h.session->textures_changed();
  }
  return py::bool_(changed);
}

py::list texture_inputs_value(const std::vector<ch::TextureInputInfo> &inputs) {
  py::list result = py::list(0);
  for (const auto &input : inputs) {
    py::list tiles = py::list(input.tiles.size());
    for (std::size_t index = 0; index < input.tiles.size(); ++index)
      tiles[index] = py::int_(input.tiles[index]);
    py::dict value = py::dict(py::arg("material_key") = py::str(input.material_key.c_str()),
                              py::arg("filename") = py::str(input.filename.c_str()),
                              py::arg("colorspace") = py::str(input.colorspace.c_str()),
                              py::arg("selection") = py::str(input.selection.c_str()),
                              py::arg("policy_id") = py::str(input.policy_id.c_str()),
                              py::arg("revision") = py::str(input.revision.c_str()), py::arg("tiles") = tiles,
                              py::arg("portable_decode_bytes") = py::int_(input.portable_decode_bytes),
                              py::arg("portable_cache_format") = py::str(input.portable_cache_format.c_str()));
    result.append(value);
  }
  return result;
}

void point_instance_status(py::dict result, std::array<std::uint64_t, 3> counts, double seconds) {
  for (const auto &[name, value] :
       {std::pair{"point_instance_syntheses", counts[0]}, std::pair{"point_instance_cache_hits", counts[1]},
        std::pair{"point_instances_synthesized", counts[2]}}) {
    py::int_ field = py::int_(value);
    result[name] = field;
  }
  result["point_instance_seconds"] = py::float_(seconds);
}

// Test/diagnostic hook: the synthetic packed table of one instancing request at one time
// (prototype slot and full packed transform, row-major, row vectors; instancer OBJ excluded).
py::object point_instance_table(py::handle data_arg, py::handle time_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Point instancing requires Houdini's host thread");
  py::handle data;
  double time;
  data = data_arg;
  time = argument_real<double>(time_arg);
  const auto request = geometry_request(data);
  if (!request.point_instancing)
    throw std::invalid_argument("The request has no point instancing");
  auto *sop = OPgetDirector()->findSOPNode(request.sop_path.c_str());
  if (!sop)
    throw std::invalid_argument("Missing SOP: " + request.sop_path);
  ch::NativePointInstanceCache cache;
  cache.begin_capture();
  const auto sample = cache.synthesize(*sop, request, time, nullptr, nullptr);
  py::list rows = py::list(0);
  if (const auto *gdp = sample->handle.gdp()) {
    GA_ROHandleI slots(gdp->findPrimitiveAttribute(ch::point_prototype_attribute));
    for (GA_Iterator it(gdp->getPrimitiveRange()); !it.atEnd(); ++it) {
      const auto *primitive = dynamic_cast<const GU_PrimPacked *>(gdp->getPrimitive(*it));
      if (!primitive)
        continue;
      UT_Matrix4D m;
      primitive->getFullTransform4(m);
      py::list matrix = py::list(16);
      for (int i = 0; i < 16; ++i)
        matrix[i] = py::float_(m(i / 4, i % 4));
      py::dict row =
          py::dict(py::arg("point") = py::int_(static_cast<long long>(gdp->pointIndex(primitive->getPointOffset(0)))),
                   py::arg("prototype") = py::int_(slots.isValid() ? slots.get(*it) : -1), py::arg("matrix") = matrix);
      rows.append(row);
    }
  }
  for (const auto &direct : sample->direct) {
    py::list matrix = py::list(16);
    for (int row = 0; row < 4; ++row)
      for (int col = 0; col < 4; ++col)
        matrix[4 * row + col] = py::float_(col < 3 ? direct.transform[4 * col + row] : (row == 3 ? 1 : 0));
    py::dict row = py::dict(py::arg("point") = py::int_(static_cast<long long>(direct.point)),
                            py::arg("prototype") = py::int_(int(direct.prototype)), py::arg("matrix") = matrix);
    py::dict attributes;
    for (const auto &attribute : direct.attributes)
      attributes[py::str(attribute.name)] =
          py::dict(py::arg("components") = attribute.components,
                   py::arg("value") = py::make_tuple(attribute.value.x, attribute.value.y, attribute.value.z));
    row["identity"] = direct.identity;
    row["attributes"] = attributes;
    row["material"] = direct.binding.path;
    row["overrides"] = py::cast(direct.binding.overrides);
    row["velocity"] = py::make_tuple(direct.velocity.x, direct.velocity.y, direct.velocity.z);
    rows.append(row);
  }
  py::dict result =
      py::dict(py::arg("instances") = rows, py::arg("warning") = py::str(sample->warning.c_str()),
               py::arg("count") = py::int_(static_cast<unsigned long long>(sample->instances)),
               py::arg("synthetic_points") =
                   py::int_(static_cast<long long>(sample->handle.gdp() ? sample->handle.gdp()->getNumPoints() : 0)));
  source_diagnostic_metadata(result, sample->diagnostics);
  return result;
}

py::object texture_inputs(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &h = handle(capsule);
  std::vector<ch::TextureInputInfo> inputs;
  {
    ReleaseGIL release;
    inputs = h.session->texture_inputs();
  }
  return texture_inputs_value(inputs);
}

py::object engine_create(py::handle capture_arg) {
  py::handle capture;
  capture = capture_arg;
  const auto &host = handle(capture);
  auto h = std::make_unique<Handle>();
  {
    std::lock_guard lock(host.state_mutex);
    if (host.capture_retired || host.closing || host.closed)
      throw std::logic_error("Native capture resources were retired");
    h->width = host.width;
    h->height = host.height;
    h->config = host.config;
  }
  h->config.persistent_updates = true;
  h->policy_slot = std::make_shared<PolicySlot>();
  h->policy_slot->replace(std::make_shared<const ch::OwnedTexturePolicy>());
  const auto slot = h->policy_slot;
  h->config.texture_input_policy.identity = [slot]() { return slot->get()->identity; };
  h->config.texture_input_policy.resolve_auto = [slot](const ch::TextureHeader &header) {
    return ch::snapshot_texture_policy(slot->get()).resolve_auto(header);
  };
  h->config.texture_input_policy.resolve_image = [slot](const std::string &source, double time) {
    return ch::snapshot_texture_policy(slot->get()).resolve_image(source, time);
  };
  {
    ReleaseGIL release;
    h->session = std::make_unique<ch::RenderSession>(h->config);
  }
  auto result = py::capsule(h.get(), "cycles_houdini.RenderSession", destroy_owned<Handle>);
  if (result)
    h.release();
  return result;
}
} // namespace cycles_houdini::python_detail
