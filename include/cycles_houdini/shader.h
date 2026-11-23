// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstdint>
#include <bit>
#include <type_traits>
#include <array>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <variant>
#include <vector>

namespace cycles_houdini {
using ShaderValue = std::variant<bool, int, float, std::array<float, 3>, std::string, std::vector<int>>;
// Shader hashes can distinguish numerically equal float representations.
inline bool float_bits_equal(float a, float b) {
  return std::bit_cast<std::uint32_t>(a) == std::bit_cast<std::uint32_t>(b);
}
inline bool shader_value_equal(const ShaderValue &a, const ShaderValue &b) {
  if (a.index() != b.index())
    return false;
  return std::visit(
      [&](const auto &value) {
        using T = std::remove_cvref_t<decltype(value)>;
        const auto &other = std::get<T>(b);
        if constexpr (std::is_same_v<T, float>)
          return float_bits_equal(value, other);
        else if constexpr (std::is_same_v<T, std::array<float, 3>>) {
          for (std::size_t i = 0; i < 3; ++i)
            if (!float_bits_equal(value[i], other[i]))
              return false;
          return true;
        } else
          return value == other;
      },
      a);
}
struct ShaderNodeData {
  std::string id, definition;
  std::map<std::string, ShaderValue> parameters;
  bool operator==(const ShaderNodeData &other) const {
    if (id != other.id || definition != other.definition || parameters.size() != other.parameters.size())
      return false;
    auto next = other.parameters.begin();
    for (const auto &[name, value] : parameters) {
      if (name != next->first || !shader_value_equal(value, next->second))
        return false;
      ++next;
    }
    return true;
  }
};
struct ShaderEndpoint {
  std::string node, socket;
  bool operator==(const ShaderEndpoint &) const = default;
};
struct ShaderLink {
  ShaderEndpoint from, to;
  bool operator==(const ShaderLink &) const = default;
};
enum class DisplacementMethod { Bump, True, Both };
// Per-material engine settings (Blender: Material > Settings). Values mirror
// ccl::EmissionSampling, ccl::VolumeSampling and ccl::VolumeInterpolation of
// the pinned engine; the defaults are the engine's Shader socket defaults.
enum class EmissionSampling { None, Auto, Front, Back, FrontBack };
enum class VolumeSampling { Distance, Equiangular, MultipleImportance };
enum class VolumeInterpolation { Linear, Cubic };
struct MaterialSettings {
  EmissionSampling emission_sampling{EmissionSampling::Auto};
  bool transparent_shadows{true};
  bool bump_map_correction{true};
  VolumeSampling volume_sampling{VolumeSampling::MultipleImportance};
  VolumeInterpolation volume_interpolation{VolumeInterpolation::Linear};
  float volume_step_rate{1.0f};
  bool operator==(const MaterialSettings &) const = default;
};
struct ShaderGraphData {
  std::uint32_t schema_version{1};
  std::vector<ShaderNodeData> nodes;
  std::vector<ShaderLink> links;
  // An empty surface endpoint represents a volume-only material. The first
  // native adapter certifies one output domain per material.
  ShaderEndpoint surface;
  std::optional<ShaderEndpoint> volume, displacement;
  DisplacementMethod displacement_method{DisplacementMethod::Bump};
  bool displacement_time_dependent{}; // Host expressions need separately owned shutter graphs.
  std::optional<double> image_time;   // Owned op: image evaluation context; not an engine socket.
  MaterialSettings settings;          // Surface/volume materials only; worlds and lights keep defaults.
  bool operator==(const ShaderGraphData &) const = default;
};
struct ShaderEnumEntry {
  std::string token;
  int engine_value{};
};
struct ShaderSocketDefinition {
  std::string id, label, type, default_json;
  // Scalar/float3 defaults for host authoring. Arrays and resource pointers
  // require authored adapters; their catalogue representation remains JSON.
  std::optional<ShaderValue> authoring_default;
  bool linkable{}, internal_svm{}, internal_osl{};
  std::vector<std::string> default_links;
  std::vector<ShaderEnumEntry> enum_values;
};
struct ShaderDefinition {
  std::string id, engine_type;
  std::uint32_t definition_version{1};
  std::vector<ShaderSocketDefinition> inputs, outputs;
};
// Catalogue only: reflection does not certify Houdini authoring, coordinate,
// ramp, texture or graph conversion semantics. Private engine types stay here.
std::vector<ShaderDefinition> shader_definitions();
std::string shader_catalogue_json();
// Keep only displacement dependencies and a neutral unbound surface root.
ShaderGraphData displacement_snapshot(ShaderGraphData graph);
// Names needed by connected surface/displacement/AOV branches only. No host
// data or engine node is retained by this dependency query.
std::set<std::string> shader_attribute_requests(const ShaderGraphData &graph);
// Attribute names the volume part looks up as volume fields (Principled Volume,
// Attribute, Volume Info). On a polygon mesh Cycles finds none of them.
std::set<std::string> shader_volume_attribute_requests(const ShaderGraphData &graph);
// Keep intrinsic Generated coordinates resident across temporary asset recovery.
// This is not a requirement to validate user-authored source attributes.
bool shader_generated_request(const ShaderGraphData &graph);
// Intrinsic mesh data demand, separate from arbitrary user field names. Only
// registered AOV writers contribute. No graph/host owner is retained.
bool shader_pointiness_request(const ShaderGraphData &graph, const std::set<std::string> &aovs = {});
std::vector<std::string> surface_graph_definitions();
std::vector<std::string> volume_graph_definitions();
std::vector<std::string> world_graph_definitions();
std::vector<std::string> light_graph_definitions();
std::vector<std::string> shader_graph_definitions();
} // namespace cycles_houdini
