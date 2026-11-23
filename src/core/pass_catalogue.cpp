// SPDX-License-Identifier: Apache-2.0
#include "pass_catalogue.h"
#include "scene/scene.h"
#include "scene/object.h"
#include "scene/shader.h"
#include "util/murmurhash.h"
#include <algorithm>
#include <cstdio>
#include <set>
#include <stdexcept>
namespace cycles_houdini {
std::string json_string(const std::string &value) {
  std::string result = "\"";
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') {
      result += '\\';
      result += char(c);
    } else if (c < 32) {
      char encoded[7];
      std::snprintf(encoded, sizeof(encoded), "\\u%04x", unsigned(c));
      result += encoded;
    } else
      result += char(c);
  }
  return result + '"';
}
std::string crypto_hash(const std::string &name) {
  char hex[9];
  std::snprintf(hex, sizeof(hex), "%08x", ccl::util_murmur_hash3(name.data(), name.size(), 0));
  return hex;
}
void crypto_metadata(std::map<std::string, std::string> &metadata, const std::string &layer,
                     const std::set<std::string> &names) {
  std::string manifest = "{";
  for (const auto &name : names) {
    if (manifest.size() > 1)
      manifest += ',';
    manifest += json_string(name) + ":" + json_string(crypto_hash(name));
  }
  manifest += '}';
  const auto prefix = "cryptomatte/" + crypto_hash(layer).substr(0, 7) + "/";
  metadata[prefix + "name"] = layer;
  metadata[prefix + "hash"] = "MurmurHash3_32";
  metadata[prefix + "conversion"] = "uint32_to_float32";
  metadata[prefix + "manifest"] = std::move(manifest);
}
std::shared_ptr<const std::map<std::string, std::string>> crypto_scene_metadata(ccl::Scene *scene,
                                                                                const CryptomatteSettings &settings) {
  if (!settings.object && !settings.material && !settings.asset)
    return {};
  auto result = std::make_shared<std::map<std::string, std::string>>();
  std::set<std::string> objects, materials, assets;
  for (const auto *object : scene->objects) {
    objects.insert(object->name.string());
    assets.insert(object->get_asset_name().string());
  }
  for (const auto *shader : scene->shaders)
    materials.insert(shader->name.string());
  if (settings.object)
    crypto_metadata(*result, "CryptoObject", objects);
  if (settings.material)
    crypto_metadata(*result, "CryptoMaterial", materials);
  if (settings.asset)
    crypto_metadata(*result, "CryptoAsset", assets);
  return result;
}
ccl::PassType pass_type(const std::string &id) {
  if (id == "combined" || id == "noisy_combined")
    return ccl::PASS_COMBINED;
  if (id == "denoising_albedo")
    return ccl::PASS_DENOISING_ALBEDO;
  if (id == "denoising_normal")
    return ccl::PASS_DENOISING_NORMAL;
  if (id == "denoising_depth")
    return ccl::PASS_DENOISING_DEPTH;
  if (id == "depth")
    return ccl::PASS_DEPTH;
  if (id == "normal")
    return ccl::PASS_NORMAL;
  if (id == "uv")
    return ccl::PASS_UV;
  if (id == "position")
    return ccl::PASS_POSITION;
  if (id == "sample_count")
    return ccl::PASS_SAMPLE_COUNT;
  for (const auto &descriptor : pass_catalogue())
    if (descriptor.id == id) {
      const auto *types = ccl::Pass::get_type_enum();
      if (types->exists(ccl::ustring(id)))
        return static_cast<ccl::PassType>((*types)[ccl::ustring(id)]);
    }
  throw std::invalid_argument("Unsupported pass: " + id);
}
std::vector<PassDescriptor> pass_catalogue() {
  return {{"combined", {"R", "G", "B", "A"}, Storage::Float32, true, true, true},
          {"motion", {"X", "Y", "Z", "W"}, Storage::Float32, true, false, false},
          {"denoising_depth", {"Z"}, Storage::Float32, true, false, false},
          {"depth", {"Z"}, Storage::Float32, false, false, false},
          {"normal", {"X", "Y", "Z"}, Storage::Float32, true, false, false},
          {"uv", {"U", "V", "W"}, Storage::Float32, true, false, false},
          {"position", {"X", "Y", "Z"}, Storage::Float32, false, false, false},
          {"sample_count", {"X"}, Storage::Float32, true, false, false},
          {"object_id", {"X"}, Storage::Float32, false, false, false},
          {"material_id", {"X"}, Storage::Float32, false, false, false},
          {"roughness", {"X"}, Storage::Float32, true, false, false},
          {"mist", {"X"}, Storage::Float32, true, false, false},
          {"diffuse_direct", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"diffuse_indirect", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"diffuse_color", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"glossy_direct", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"glossy_indirect", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"glossy_color", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"transmission_direct", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"transmission_indirect", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"transmission_color", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"volume_direct", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"volume_indirect", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"emission", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"background", {"R", "G", "B"}, Storage::Float32, true, true, false},
          {"ao", {"R", "G", "B"}, Storage::Float32, true, false, false},
          {"shadow_catcher", {"R", "G", "B"}, Storage::Float32, true, false, true},
          {"noisy_combined", {"R", "G", "B", "A"}, Storage::Float32, true, true, false},
          {"denoising_albedo", {"R", "G", "B"}, Storage::Float32, true, false, false},
          {"denoising_normal", {"X", "Y", "Z"}, Storage::Float32, true, false, false}};
}

void validate_passes(const std::vector<std::string> &passes) {
  if (std::count(passes.begin(), passes.end(), "combined") != 1)
    throw std::invalid_argument("Passes must contain combined exactly once");
  std::set<std::string> seen;
  for (const auto &id : passes) {
    if (!seen.insert(id).second)
      throw std::invalid_argument("Duplicate pass: " + id);
    pass_type(id);
  }
}

void validate_aovs(const std::vector<AovDefinition> &aovs) {
  if (aovs.size() > 64)
    throw std::invalid_argument("At most 64 shader AOVs are supported by this integration");
  std::set<std::string> seen;
  for (const auto &aov : aovs) {
    const auto letter = [](unsigned char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_'; };
    if (aov.name.empty() || aov.name.size() > 128 || !letter(aov.name.front()) ||
        !std::all_of(aov.name.begin(), aov.name.end(),
                     [&](unsigned char c) { return letter(c) || (c >= '0' && c <= '9'); }))
      throw std::invalid_argument("AOV name must be an ASCII identifier of 1..128 characters: " + aov.name);
    const auto catalogue = pass_catalogue();
    if (ccl::Pass::get_type_enum()->exists(ccl::ustring(aov.name)) ||
        std::any_of(catalogue.begin(), catalogue.end(), [&](const auto &p) { return p.id == aov.name; }) ||
        aov.name == "R" || aov.name == "G" || aov.name == "B" || aov.name == "A" ||
        aov.name.starts_with("CryptoObject") || aov.name.starts_with("CryptoMaterial") ||
        aov.name.starts_with("CryptoAsset"))
      throw std::invalid_argument("AOV name conflicts with a reserved pass/channel: " + aov.name);
    if (!seen.insert(aov.name).second)
      throw std::invalid_argument("Duplicate shader AOV: " + aov.name);
  }
}
std::vector<PassDescriptor> aov_descriptors(const std::vector<AovDefinition> &aovs) {
  validate_aovs(aovs);
  std::vector<PassDescriptor> result;
  for (const auto &aov : aovs)
    result.push_back({aov.name,
                      aov.color ? std::vector<std::string>{"R", "G", "B", "A"} : std::vector<std::string>{"X"},
                      Storage::Float32, true, aov.color, false});
  return result;
}

void validate_lightgroup_assignment(const std::string &name) {
  if (name.empty())
    return;
  const auto letter = [](unsigned char c) { return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '_'; };
  if (name.size() > 128 || !letter(name.front()) ||
      !std::all_of(name.begin(), name.end(), [&](unsigned char c) { return letter(c) || (c >= '0' && c <= '9'); }))
    throw std::invalid_argument("Light group must be an ASCII identifier of 1..128 characters: " + name);
}
void validate_lightgroups(const std::vector<std::string> &groups) {
  if (groups.size() > 64)
    throw std::invalid_argument("At most 64 light-group outputs are supported by this integration");
  std::set<std::string> seen;
  for (const auto &name : groups) {
    if (name.empty())
      throw std::invalid_argument("A light-group output needs a name");
    validate_lightgroup_assignment(name);
    if (!seen.insert(name).second)
      throw std::invalid_argument("Duplicate light-group output: " + name);
  }
}
void validate_pass_outputs(const std::vector<AovDefinition> &aovs, const std::vector<std::string> &groups) {
  validate_aovs(aovs);
  validate_lightgroups(groups);
  for (const auto &group : groups)
    for (const auto &aov : aovs)
      if (aov.name == "lightgroup_" + group)
        throw std::invalid_argument("Shader AOV conflicts with light-group output: " + aov.name);
}
std::vector<PassDescriptor> lightgroup_descriptors(const std::vector<std::string> &groups) {
  validate_lightgroups(groups);
  std::vector<PassDescriptor> result;
  for (const auto &name : groups)
    result.push_back({"lightgroup_" + name, {"R", "G", "B"}, Storage::Float32, true, true, false});
  return result;
}

void validate_pass_index(int index) {
  if (index < 0 || index > 32767)
    throw std::invalid_argument("Pass Index requires an integer within 0..32767");
}

void validate_cryptomatte(const CryptomatteSettings &settings) {
  if (settings.levels < 2 || settings.levels > 16)
    throw std::invalid_argument("Cryptomatte levels must be within 2..16");
}
void validate_cryptomatte_name(const std::string &name) {
  if (name.size() > 4096 || name.find('\0') != std::string::npos)
    throw std::invalid_argument("Cryptomatte names must contain at most 4096 UTF-8 bytes and no NUL");
}
std::vector<PassDescriptor> cryptomatte_descriptors(const CryptomatteSettings &settings) {
  validate_cryptomatte(settings);
  std::vector<PassDescriptor> result;
  for (const auto &[enabled, name] :
       {std::pair{settings.object, "CryptoObject"}, std::pair{settings.material, "CryptoMaterial"},
        std::pair{settings.asset, "CryptoAsset"}})
    if (enabled)
      for (int layer = 0; layer < (settings.levels + 1) / 2; ++layer) {
        const auto id = std::string(name) + "0" + std::to_string(layer);
        result.push_back({id, {"R", "G", "B", "A"}, Storage::Float32, true, false, false, true});
      }
  return result;
}

} // namespace cycles_houdini
