// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/volume.h"
#include "color_internal.h"
#include "texture_assets.h"
#include "mesh_payload.h"
#include <tbb/blocked_range.h>
#include <tbb/parallel_for.h>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <type_traits>
#include <tuple>
#include <unordered_set>
#include "util/tbb.h"

namespace cycles_houdini {
std::vector<ObjectPoseData> derive_root_poses(const std::map<std::string, std::vector<PoseOriginRecord>> &origins,
                                              const std::vector<RootPoseRecord> &roots) {
  // Same arithmetic, in the same order, as navigation_snapshot's multiply():
  // identical floats to the per-instance path. Each output slot is written by
  // one task only, and the result is sorted by key: deterministic.
  const auto multiply = [](const Matrix &a, const Matrix &b) {
    Matrix c{};
    for (int row = 0; row < 3; ++row)
      for (int col = 0; col < 4; ++col) {
        for (int k = 0; k < 3; ++k)
          c[row * 4 + col] += a[row * 4 + k] * b[k * 4 + col];
        if (col == 3)
          c[row * 4 + col] += a[row * 4 + 3];
      }
    return c;
  };
  std::vector<std::pair<const RootPoseRecord *, const PoseOriginRecord *>> work;
  for (const auto &root : roots) {
    const auto found = origins.find(root.key);
    if (found == origins.end())
      throw std::invalid_argument("Root pose without origins on the renderer: " + root.key);
    for (const auto &origin : found->second)
      work.emplace_back(&root, &origin);
  }
  std::vector<ObjectPoseData> result(work.size());
  tbb::parallel_for(
      tbb::blocked_range<std::size_t>(0, work.size(), 1024), [&](const tbb::blocked_range<std::size_t> &r) {
        for (std::size_t n = r.begin(); n != r.end(); ++n) {
          const auto &root = *work[n].first;
          const auto &origin = *work[n].second;
          if (root.motion.size() != origin.motion.size())
            throw std::invalid_argument("Root pose shutter does not match its origins: " + root.key);
          auto &object = result[n];
          object.key = origin.key;
          object.transform =
              multiply(root.centre, origin.motion.empty() ? origin.local : origin.motion[origin.motion.size() / 2]);
          for (std::size_t i = 0; i < origin.motion.size(); ++i)
            object.motion.push_back(multiply(root.motion[i], origin.motion[i]));
          if (std::all_of(object.motion.begin(), object.motion.end(),
                          [&](const auto &pose) { return pose == object.transform; }))
            object.motion.clear();
          if (origin.nominal)
            object.nominal_transform = multiply(root.nominal, origin.local);
        }
      });
  // One root (origins sorted by key) yields sorted output already; is_sorted is one pass, sort is n log n.
  const auto by_key = [](const auto &a, const auto &b) { return a.key < b.key; };
  if (!std::is_sorted(result.begin(), result.end(), by_key))
    std::sort(result.begin(), result.end(), by_key);
  for (std::size_t n = 1; n < result.size(); ++n)
    if (result[n - 1].key == result[n].key)
      throw std::invalid_argument("Object owned by two pose roots: " + result[n].key);
  return result;
}
namespace {
template <class Map, class Values> void insert_values(Map &map, const Values &values) {
  std::set<std::string> keys;
  for (const auto &value : values) {
    const auto &key = value.key;
    if (key.empty() || !keys.insert(key).second)
      throw std::invalid_argument("Duplicate/empty snapshot resource key");
    map[key] = value;
  }
}
// SOP time sampling can produce new data IDs for identical render payloads.
// Compare only newly captured owned arrays, on the converter thread, before
// replacing a snapshot resource. Unchanged cached pointers need no scan and
// no repeated geometry packet/GPU upload. Volume grids retain their own policy.
auto geometry_payload(const MeshData &v) {
  return std::tie(v.key, v.positions, v.triangles, v.corner_normals, v.corner_uv, v.corner_colors, v.materials,
                  v.triangle_material, v.smooth, v.motion, v.motion_corner_normals, v.motion_corner_uv,
                  v.motion_corner_colors, v.motion_uv_complete, v.motion_color_complete, v.subdivision, v.nominal_pose,
                  v.motion_blur, v.attributes, v.instance_attributes, v.pointiness, v.texture_space, v.blender_edges,
                  v.blender_vertex_normals);
}
auto geometry_payload(const PointData &v) {
  return std::tie(v.key, v.material, v.materials, v.material_slots, v.positions, v.radii, v.colors, v.randoms, v.uv,
                  v.lifetimes, v.splats, v.motion, v.generated_attribute, v.attributes, v.motion_colors, v.motion_uv,
                  v.motion_color_complete, v.motion_uv_complete, v.instance_attributes);
}
auto geometry_payload(const CurveData &v) {
  return std::tie(v.key, v.material, v.materials, v.material_slots, v.positions, v.radii, v.first_key, v.colors,
                  v.randoms, v.uv, v.motion, v.shape, v.generated, v.generated_attribute, v.attributes, v.motion_colors,
                  v.motion_uv, v.motion_color_complete, v.motion_uv_complete, v.instance_attributes);
}
template <class Map, class Values> void insert_geometry(Map &map, const Values &values) {
  std::set<std::string> keys;
  for (const auto &value : values) {
    if (!value || value->key.empty() || !keys.insert(value->key).second)
      throw std::invalid_argument("Invalid snapshot geometry key");
    if constexpr (requires { geometry_payload(*value); }) {
      const auto previous = map.find(value->key);
      if (previous != map.end() && (previous->second == value ||
                                    mesh_payload_equal(geometry_payload(*previous->second), geometry_payload(*value))))
        continue;
    }
    map[value->key] = value;
  }
}
template <class Map, class Deleted> void erase_values(Map &map, const Deleted &values) {
  for (const auto &key : values)
    map.erase(key);
}
template <class Map, class Output> void difference(const Map *previous, const Map &desired, Output &output) {
  for (const auto &[key, value] : desired) {
    bool same = false;
    if (previous) {
      const auto old = previous->find(key);
      if (old != previous->end()) {
        if constexpr (requires { value->revision; })
          same = value == old->second;
        else if constexpr (requires { value == old->second; })
          same = value == old->second;
        else
          same = value.geometry_key == old->second.geometry_key && value.transform == old->second.transform &&
                 value.visibility == old->second.visibility && value.random_id == old->second.random_id &&
                 value.motion == old->second.motion;
      }
    }
    if (!same)
      output.push_back(value);
  }
}
template <class Map> void deletions(const Map *previous, const Map &desired, std::vector<std::string> &output) {
  if (previous)
    for (const auto &[key, value] : *previous)
      if (!desired.contains(key))
        output.push_back(key);
}
bool same_crop(const std::optional<Crop> &a, const std::optional<Crop> &b) {
  return (!a && !b) || (a && b && a->x == b->x && a->y == b->y && a->width == b->width && a->height == b->height);
}
} // namespace
std::shared_ptr<const SceneSnapshotState> snapshot_state(const std::shared_ptr<const SceneSnapshotState> &previous,
                                                         const SceneUpdate &u) {
  return snapshot_state(previous, u, nullptr);
}
std::shared_ptr<const SceneSnapshotState> snapshot_state(const std::shared_ptr<const SceneSnapshotState> &previous,
                                                         const SceneUpdate &u,
                                                         std::shared_ptr<const SceneSnapshotState::ObjectTable> table) {
  if (previous && !previous->objects)
    throw std::logic_error("Scene snapshot has no owned object table");
  auto result = previous ? std::make_shared<SceneSnapshotState>(*previous) : std::make_shared<SceneSnapshotState>();
  if (table)
    result->objects = std::move(table);
  else if (!u.objects.empty() || !u.object_poses.empty() || !u.object_attributes.empty() ||
           !u.deleted_objects.empty()) {
    // Construct each immutable record once. Cloning the complete old table
    // before replacing records repeated all binding strings on large moves.
    const auto ordered = [](const auto &values) {
      using Value = typename std::remove_reference_t<decltype(values)>::value_type;
      std::vector<const Value *> sorted;
      sorted.reserve(values.size());
      for (const auto &value : values)
        sorted.push_back(&value);
      const auto less = [](const auto *a, const auto *b) { return a->key < b->key; };
      if (!std::is_sorted(sorted.begin(), sorted.end(), less))
        std::sort(sorted.begin(), sorted.end(), less);
      for (std::size_t n = 0; n < sorted.size(); ++n)
        if (sorted[n]->key.empty() || (n && sorted[n - 1]->key == sorted[n]->key))
          throw std::invalid_argument("Duplicate/empty snapshot object, pose or attribute key");
      return sorted;
    };
    const auto incoming = ordered(u.objects);
    const auto poses = ordered(u.object_poses);
    const auto attributes = ordered(u.object_attributes);
    for (const auto *value : attributes)
      if (value->pose && (value->pose->key.empty() || value->pose->key != value->key))
        throw std::invalid_argument("Attached object pose key does not match its attribute command: " + value->key);
    std::unordered_set<std::string_view> deleted;
    deleted.reserve(u.deleted_objects.size());
    for (const auto &key : u.deleted_objects)
      deleted.insert(key);
    auto objects = std::make_shared<SceneSnapshotState::ObjectTable>();
    // The ordered walk below validates and plans every record; the records
    // themselves are then built in parallel into their own slots and moved
    // into the table in walk order.
    struct Plan {
      const ObjectData *value{};
      const ObjectPoseData *pose{};
      const ObjectAttributeData *fields{};
    };
    std::vector<Plan> plans;
    plans.reserve(result->objects->size() + incoming.size());
    auto old = result->objects->begin();
    std::size_t added = 0, posed = 0, attributed = 0;
    while (old != result->objects->end() || added < incoming.size()) {
      const bool replacement =
          added < incoming.size() && (old == result->objects->end() || incoming[added]->key <= old->first);
      const auto &value = replacement ? *incoming[added] : old->second;
      const auto &key = value.key;
      if (posed < poses.size() && poses[posed]->key < key)
        throw std::invalid_argument("Pose references a missing snapshot object: " + poses[posed]->key);
      if (attributed < attributes.size() && attributes[attributed]->key < key)
        throw std::invalid_argument("Attributes reference a missing snapshot object: " + attributes[attributed]->key);
      const bool pose = posed < poses.size() && poses[posed]->key == key;
      const bool fields = attributed < attributes.size() && attributes[attributed]->key == key;
      if (pose && (replacement || deleted.contains(key)))
        throw std::invalid_argument("Object pose conflicts with an upsert/deletion: " + key);
      if (fields && (replacement || pose || deleted.contains(key)))
        throw std::invalid_argument("Object attributes conflict with an upsert, pose or deletion: " + key);
      if (replacement || !deleted.contains(key))
        plans.push_back({&value, pose ? poses[posed] : nullptr, fields ? attributes[attributed] : nullptr});
      if (pose)
        ++posed;
      if (fields)
        ++attributed;
      if (old != result->objects->end() && old->first == key)
        ++old;
      if (replacement)
        ++added;
    }
    if (posed != poses.size())
      throw std::invalid_argument("Pose references a missing snapshot object: " + poses[posed]->key);
    if (attributed != attributes.size())
      throw std::invalid_argument("Attributes reference a missing snapshot object: " + attributes[attributed]->key);
    std::vector<std::pair<std::string, ObjectData>> records(plans.size());
    const auto build = [&](std::size_t begin, std::size_t end) {
      for (auto index = begin; index < end; ++index) {
        const auto &plan = plans[index];
        const auto &value = *plan.value;
        auto &[key, next] = records[index];
        key = value.key;
        if (plan.pose) {
          next = value;
          next.transform = plan.pose->transform;
          next.motion = plan.pose->motion;
          next.nominal_transform = plan.pose->nominal_transform;
        } else if (plan.fields) {
          const auto &command = *plan.fields;
          next =
              ObjectData{value.key,    value.geometry_key, value.transform,         value.visibility,  value.random_id,
                         value.motion, value.properties,   value.nominal_transform, command.attributes};
          if (command.pose) {
            next.transform = command.pose->transform;
            next.motion = command.pose->motion;
            next.nominal_transform = command.pose->nominal_transform;
          }
        } else
          next = value;
      }
    };
    const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
    if (records.size() < 4096 || (serial && std::string_view(serial) == "1"))
      build(0, records.size());
    else
      tbb::parallel_for(tbb::blocked_range<std::size_t>(0, records.size(), 1024),
                        [&](const auto &range) { build(range.begin(), range.end()); });
    for (auto &[key, next] : records)
      objects->emplace_hint(objects->end(), std::move(key), std::move(next));
    result->objects = std::move(objects);
  }
  erase_values(result->materials, u.deleted_materials);
  erase_values(result->lights, u.deleted_lights);
  for (const auto &key : u.deleted_geometry) {
    result->meshes.erase(key);
    result->volumes.erase(key);
    result->points.erase(key);
    result->curves.erase(key);
  }
  insert_values(result->materials, u.materials);
  insert_values(result->lights, u.lights);
  // One geometry key has exactly one primitive type in the complete state.
  std::set<std::string> geometry_keys;
  const auto clear_types = [&](const auto &values, int kind) {
    for (const auto &value : values) {
      if (!value || value->key.empty() || !geometry_keys.insert(value->key).second)
        throw std::invalid_argument("Duplicate snapshot geometry across primitive types");
      if (kind != 0)
        result->meshes.erase(value->key);
      if (kind != 1)
        result->volumes.erase(value->key);
      if (kind != 2)
        result->points.erase(value->key);
      if (kind != 3)
        result->curves.erase(value->key);
    }
  };
  clear_types(u.meshes, 0);
  clear_types(u.volumes, 1);
  clear_types(u.points, 2);
  clear_types(u.curves, 3);
  insert_geometry(result->meshes, u.meshes);
  insert_geometry(result->volumes, u.volumes);
  insert_geometry(result->points, u.points);
  insert_geometry(result->curves, u.curves);
  const auto assign = [](auto &target, const auto &value) {
    if (value)
      target = value;
  };
  assign(result->linking, u.linking);
  assign(result->camera, u.camera);
  assign(result->dicing, u.dicing);
  assign(result->crop, u.crop);
  assign(result->background, u.background);
  assign(result->denoising, u.denoising);
  assign(result->world, u.world);
  assign(result->source_diagnostics, u.source_diagnostics);
  assign(result->rendering, u.rendering);
  assign(result->passes, u.passes);
  assign(result->aovs, u.aovs);
  assign(result->lightgroups, u.lightgroups);
  assign(result->background_lightgroup, u.background_lightgroup);
  assign(result->background_shadow_catcher, u.background_shadow_catcher);
  assign(result->cryptomatte, u.cryptomatte);
  assign(result->transparent_background, u.transparent_background);
  assign(result->transparent_glass, u.transparent_glass);
  assign(result->transparent_roughness_threshold, u.transparent_roughness_threshold);
  assign(result->motion_blur, u.motion_blur);
  result->defer_camera_subdivision = u.defer_camera_subdivision;
  return result;
}
SceneUpdate snapshot_difference(const SceneSnapshotState *old, const SceneSnapshotState &state) {
  if (!state.objects || (old && !old->objects))
    throw std::logic_error("Scene snapshot has no owned object table");
  SceneUpdate result;
#define CH_DIFF(member) difference(old ? &old->member : nullptr, state.member, result.member)
  CH_DIFF(materials);
  CH_DIFF(meshes);
  CH_DIFF(volumes);
  CH_DIFF(points);
  CH_DIFF(curves);
  CH_DIFF(lights);
#undef CH_DIFF
  deletions(old ? &old->materials : nullptr, state.materials, result.deleted_materials);
  // Equal ownership means equal immutable contents, not a probabilistic hash.
  // Different tables still receive the complete value/deletion comparison.
  if (!old || old->objects != state.objects) {
    const auto end = old ? old->objects->end() : state.objects->end();
    auto previous = old ? old->objects->begin() : end, current = state.objects->begin();
    // Both maps are ordered. Compare their contents and deletions in one
    // linear walk, including independently allocated but equivalent tables.
    while (previous != end || current != state.objects->end()) {
      if (current == state.objects->end() || (previous != end && previous->first < current->first)) {
        result.deleted_objects.push_back(previous->first);
        ++previous;
        continue;
      }
      const auto &[key, object] = *current;
      if (previous != end && previous->first == key) {
        const auto &before = previous->second;
        switch (classify_object_update(before, object)) {
        case ObjectUpdateKind::Unchanged:
          break;
        case ObjectUpdateKind::Pose:
          result.object_poses.push_back({key, object.transform, object.motion, object.nominal_transform});
          break;
        case ObjectUpdateKind::Attributes:
          result.object_attributes.push_back({key, object.attributes});
          break;
        case ObjectUpdateKind::PoseAttributes:
          result.object_attributes.push_back(
              {key, object.attributes, ObjectPoseData{key, object.transform, object.motion, object.nominal_transform}});
          break;
        case ObjectUpdateKind::Full:
          result.objects.push_back(object);
          break;
        }
        ++previous;
      } else
        result.objects.push_back(object);
      ++current;
    }
  }
  deletions(old ? &old->lights : nullptr, state.lights, result.deleted_lights);
  // Type replacement uses the engine's existing same-key replacement route,
  // not a simultaneous upsert/deletion of the same geometry key.
  if (old) {
    std::set<std::string> previous, current;
    const auto keys = [](const auto &map, auto &target) {
      for (const auto &[key, value] : map)
        target.insert(key);
    };
    keys(old->meshes, previous);
    keys(old->volumes, previous);
    keys(old->points, previous);
    keys(old->curves, previous);
    keys(state.meshes, current);
    keys(state.volumes, current);
    keys(state.points, current);
    keys(state.curves, current);
    for (const auto &key : previous)
      if (!current.contains(key))
        result.deleted_geometry.push_back(key);
  }
#define CH_OPTION(member)                                                                                              \
  if (!old || old->member != state.member)                                                                             \
  result.member = state.member
  CH_OPTION(camera);
  CH_OPTION(dicing);
  CH_OPTION(background);
  CH_OPTION(denoising);
  CH_OPTION(rendering);
  CH_OPTION(passes);
  CH_OPTION(aovs);
  CH_OPTION(lightgroups);
  CH_OPTION(cryptomatte);
  CH_OPTION(background_lightgroup);
  CH_OPTION(transparent_background);
  CH_OPTION(transparent_glass);
  CH_OPTION(transparent_roughness_threshold);
  CH_OPTION(motion_blur);
  CH_OPTION(background_shadow_catcher);
  CH_OPTION(world);
  CH_OPTION(linking);
  CH_OPTION(source_diagnostics);
#undef CH_OPTION
  if (!old || !same_crop(old->crop, state.crop))
    result.crop = state.crop;
  result.defer_camera_subdivision = state.defer_camera_subdivision;
  return result;
}
struct SnapshotTextureResolver::Impl {
  TextureInputPolicy host;
  struct Cache {
    TextureAsset asset;
    std::vector<TexturePolicyEntry> entries;
  };
  std::map<std::string, Cache> cache;
};
SnapshotTextureResolver::SnapshotTextureResolver(TextureInputPolicy host) : impl_(std::make_unique<Impl>()) {
  impl_->host = std::move(host);
}
SnapshotTextureResolver::~SnapshotTextureResolver() = default;
OwnedTexturePolicy SnapshotTextureResolver::capture(const SceneSnapshotState &state) {
  OwnedTexturePolicy result;
  result.identity = impl_->host.identity ? impl_->host.identity() : std::string{};
  std::set<std::string> used;
  std::vector<const ShaderGraphData *> graphs;
  for (const auto &[key, material] : state.materials) {
    if (material.graph)
      graphs.push_back(&*material.graph);
    for (const auto &sample : material.displacement_samples)
      graphs.push_back(&sample);
  }
  if (state.world && state.world->graph)
    graphs.push_back(&*state.world->graph);
  for (const auto &[key, light] : state.lights)
    if (light.graph)
      graphs.push_back(&*light.graph);
  for (const auto *graph : graphs)
    for (const auto &node : graph->nodes) {
      if (node.definition != "cycles/image_texture/1" && node.definition != "cycles/environment_texture/1")
        continue;
      const auto file = node.parameters.find("filename");
      if (file == node.parameters.end())
        continue;
      const auto *filename = std::get_if<std::string>(&file->second);
      if (!filename)
        continue;
      if (filename->starts_with("op:"))
        continue; // Owned rasters are captured separately from file headers.
      const auto input = node.parameters.find("colorspace");
      if (input != node.parameters.end()) {
        const auto *token = std::get_if<std::string>(&input->second);
        if (!token || (!token->empty() && *token != "auto"))
          continue;
      }
      std::vector<int> tiles;
      const auto tiled = node.parameters.find("tiles");
      if (tiled != node.parameters.end())
        if (const auto *values = std::get_if<std::vector<int>>(&tiled->second))
          tiles = *values;
      std::string identity = *filename;
      for (int tile : tiles)
        identity += ':' + std::to_string(tile);
      used.insert(identity);
      auto found = impl_->cache.find(identity);
      if (found == impl_->cache.end() || texture_asset_changed(found->second.asset, impl_->host)) {
        Impl::Cache entry;
        TextureInputPolicy recording;
        recording.identity = [&]() { return result.identity; };
        recording.resolve_auto = [&](const TextureHeader &header) {
          if (!impl_->host.resolve_auto)
            throw std::invalid_argument("Queued native Auto requires resolved host texture policy");
          const auto selected = impl_->host.resolve_auto(header);
          entry.entries.push_back({header, selected});
          return selected;
        };
        try {
          entry.asset = prepare_texture_asset(*filename, "auto", tiles, recording);
        } catch (const TextureSourceError &) {
          // The renderer applies the same typed fallback. Keep successful
          // policy entries out of a partially resolved failed image.
          impl_->cache.erase(identity);
          continue;
        }
        found = impl_->cache.insert_or_assign(identity, std::move(entry)).first;
      }
      for (const auto &entry : found->second.entries) {
        const auto existing = std::find_if(result.entries.begin(), result.entries.end(), [&](const auto &value) {
          return value.header.filename == entry.header.filename;
        });
        if (existing == result.entries.end())
          result.entries.push_back(entry);
        else if (existing->header != entry.header || existing->selection != entry.selection)
          throw std::invalid_argument("Conflicting queued texture policy for one file");
      }
    }
  for (auto it = impl_->cache.begin(); it != impl_->cache.end();)
    if (!used.contains(it->first))
      it = impl_->cache.erase(it);
    else
      ++it;
  return result;
}
TextureInputPolicy snapshot_texture_policy(const std::shared_ptr<const OwnedTexturePolicy> &policy) {
  if (!policy)
    throw std::invalid_argument("Owned texture policy is missing");
  TextureInputPolicy result;
  result.identity = [policy]() { return policy->identity; };
  result.resolve_auto = [policy](const TextureHeader &header) {
    const auto entry = std::find_if(policy->entries.begin(), policy->entries.end(),
                                    [&](const auto &value) { return value.header.filename == header.filename; });
    if (entry == policy->entries.end() || entry->header != header)
      throw std::invalid_argument("Texture metadata changed after host snapshot; recapture the scene: " +
                                  header.filename);
    return entry->selection;
  };
  result.resolve_image = [policy](const std::string &source, double time) {
    const auto found = policy->images.find({source, time});
    return found == policy->images.end() ? std::shared_ptr<const TextureImageData>{} : found->second;
  };
  return result;
}
std::vector<TextureImageKey> texture_image_requests(const SceneSnapshotState &state) {
  std::set<TextureImageKey> requests;
  const auto graph = [&](const ShaderGraphData &data) {
    const auto time = data.image_time.value_or(0.);
    if (!std::isfinite(time))
      throw std::invalid_argument("Image context requires a finite evaluation time");
    std::map<std::string, std::vector<std::string>> incoming;
    for (const auto &link : data.links)
      incoming[link.to.node].push_back(link.from.node);
    std::vector<std::string> pending{data.surface.node};
    if (data.volume)
      pending.push_back(data.volume->node);
    if (data.displacement)
      pending.push_back(data.displacement->node);
    for (const auto &node : data.nodes)
      if (node.definition == "cycles/aov_output/1")
        pending.push_back(node.id);
    std::set<std::string> used;
    while (!pending.empty()) {
      auto id = std::move(pending.back());
      pending.pop_back();
      if (!used.insert(id).second)
        continue;
      for (const auto &upstream : incoming[id])
        pending.push_back(upstream);
    }
    for (const auto &node : data.nodes)
      if (used.contains(node.id) &&
          (node.definition == "cycles/image_texture/1" || node.definition == "cycles/environment_texture/1")) {
        const auto found = node.parameters.find("filename");
        if (found != node.parameters.end())
          if (const auto *name = std::get_if<std::string>(&found->second); name && name->starts_with("op:"))
            requests.insert({*name, time});
      }
  };
  for (const auto &[key, material] : state.materials) {
    if (material.graph)
      graph(*material.graph);
    for (const auto &sample : material.displacement_samples)
      graph(sample);
  }
  if (state.world && state.world->graph)
    graph(*state.world->graph);
  for (const auto &[key, light] : state.lights)
    if (light.graph)
      graph(*light.graph);
  return {requests.begin(), requests.end()};
}
std::shared_ptr<const SceneSnapshotState>
shader_resource_state(const std::shared_ptr<const SceneSnapshotState> &previous, const SceneUpdate &update) {
  if (previous && update.materials.empty() && update.deleted_materials.empty() && update.lights.empty() &&
      update.deleted_lights.empty() && !update.world && !update.motion_blur)
    return previous;
  SceneUpdate resources;
  resources.materials = update.materials;
  resources.deleted_materials = update.deleted_materials;
  resources.lights = update.lights;
  resources.deleted_lights = update.deleted_lights;
  resources.world = update.world;
  resources.motion_blur = update.motion_blur;
  return snapshot_state(previous, resources);
}
void texture_image_difference(const OwnedTexturePolicy *previous, const OwnedTexturePolicy &desired,
                              WorkerUpdate &update) {
  for (const auto &[key, image] : desired.images) {
    if (!image || key.source != image->source || key.evaluation_time != image->evaluation_time)
      throw std::logic_error("Owned image ledger has inconsistent identity");
    const auto old = previous ? previous->images.find(key) : desired.images.end();
    if (!previous || old == previous->images.end() || !old->second || old->second->revision != image->revision)
      update.image_upserts.push_back(image);
  }
  if (previous)
    for (const auto &[key, image] : previous->images)
      if (!desired.images.contains(key))
        update.deleted_images.push_back(key);
}
void validate_texture_image_references(const SceneSnapshotState &state, const OwnedTexturePolicy &policy) {
  const auto requested = texture_image_requests(state);
  if (requested.size() != policy.images.size())
    throw std::invalid_argument("Worker image ledger does not match referenced material resources");
  for (const auto &key : requested)
    if (!policy.images.contains(key))
      throw std::invalid_argument("Worker image ledger is missing a referenced material resource");
}
std::shared_ptr<const OwnedTexturePolicy>
apply_texture_image_difference(const std::shared_ptr<const OwnedTexturePolicy> &previous, WorkerUpdate &update) {
  if (!update.textures.images.empty())
    throw std::invalid_argument("Worker image payload must use the revisioned resource delta");
  auto result = std::make_shared<OwnedTexturePolicy>(std::move(update.textures));
  if (previous)
    result->images = previous->images;
  std::set<TextureImageKey> touched;
  for (const auto &key : update.deleted_images) {
    if (!key.source.starts_with("op:") || key.source.find('\0') != std::string::npos ||
        !std::isfinite(key.evaluation_time) || !touched.insert(key).second)
      throw std::invalid_argument("Worker image deletion has an invalid/duplicate identity");
    if (!result->images.erase(key))
      throw std::invalid_argument("Worker image deletion references a missing resource");
  }
  for (const auto &image : update.image_upserts) {
    if (!image)
      throw std::invalid_argument("Worker image upsert has no owned resource");
    validate_texture_image(*image);
    const TextureImageKey key{image->source, image->evaluation_time};
    if (!touched.insert(key).second)
      throw std::invalid_argument("Worker image upsert conflicts with another resource operation");
    if (texture_image_revision(*image) != image->revision)
      throw std::invalid_argument("Worker image revision does not match its pixel contents");
    result->images[key] = image;
  }
  return result;
}
OwnedColorPolicy snapshot_color_policy(const std::shared_ptr<const ColorConfiguration> &color) {
  if (!color)
    throw std::invalid_argument("Color snapshot is missing");
  const auto config = owned_color_configuration(*color);
  OwnedColorPolicy result;
  result.config_path = color_configuration_path(*color);
  if (!result.config_path.empty()) {
    const auto path = std::filesystem::absolute(std::filesystem::u8path(result.config_path)).lexically_normal();
    if (!std::filesystem::is_regular_file(path))
      throw std::invalid_argument("Houdini OCIO configuration is not a readable file: " + result.config_path);
    const auto utf8 = path.generic_u8string();
    result.config_path.assign(utf8.begin(), utf8.end());
  } else {
    std::ostringstream serialized;
    config->serialize(serialized);
    result.yaml = serialized.str();
  }
  result.working_directory = config->getWorkingDir();
  result.render_policy_id = color->policy().render_policy_id;
  const auto context = config->getCurrentContext();
  for (int i = 0; i < context->getNumStringVars(); ++i) {
    const auto *name = context->getStringVarNameByIndex(i);
    result.context.emplace(name, context->getStringVar(name));
  }
  return result;
}
void activate_snapshot_color_policy(const OwnedColorPolicy &policy) {
  const auto check_text = [](const std::string &text) {
    if (text.find('\0') != std::string::npos)
      throw std::invalid_argument("Worker color policy contains invalid text");
  };
  check_text(policy.config_path);
  check_text(policy.working_directory);
  if (policy.config_path.empty() == policy.yaml.empty())
    throw std::invalid_argument("Worker color policy requires exactly one config file or in-memory definition");
  // This runs only in the isolated renderer. OCIO loads an explicit context
  // through its supported environment API; never mutate its const Context.
  for (const auto &[key, value] : policy.context) {
    check_text(key);
    check_text(value);
    if (key.empty() || key.find('=') != std::string::npos)
      throw std::invalid_argument("Worker color context variable has an invalid name");
#ifdef _WIN32
    if (_putenv_s(key.c_str(), value.c_str()) != 0)
      throw std::runtime_error("Could not restore worker color context");
#else
    if (setenv(key.c_str(), value.c_str(), 1) != 0)
      throw std::runtime_error("Could not restore worker color context");
#endif
  }
  OCIO_NAMESPACE::ConfigRcPtr config;
  // The caller has joined its isolated renderer. No artist-process OCIO
  // pointer or another worker's cache is changed by this refresh.
  OCIO_NAMESPACE::ClearAllCaches();
  if (!policy.config_path.empty()) {
    if (!std::filesystem::path(std::filesystem::u8path(policy.config_path)).is_absolute())
      throw std::invalid_argument("Worker OCIO config filename must be absolute");
#ifdef _WIN32
    if (_putenv_s("OCIO", policy.config_path.c_str()) != 0)
#else
    if (setenv("OCIO", policy.config_path.c_str(), 1) != 0)
#endif
      throw std::runtime_error("Could not select worker OCIO configuration file");
    config = OCIO_NAMESPACE::Config::CreateFromFile(policy.config_path.c_str())->createEditableCopy();
  } else {
    std::istringstream source(policy.yaml);
    config = OCIO_NAMESPACE::Config::CreateFromStream(source)->createEditableCopy();
  }
  config->setWorkingDir(policy.working_directory.c_str());
  config->clearEnvironmentVars();
  for (const auto &[key, value] : policy.context)
    config->addEnvironmentVar(key.c_str(), value.c_str());
  config->setEnvironmentMode(OCIO_NAMESPACE::ENV_ENVIRONMENT_LOAD_PREDEFINED);
  config->loadEnvironment();
  set_color_configuration_path_resolver([path = policy.config_path]() { return path; });
  OCIO_NAMESPACE::SetCurrentConfig(config);
  if (current_color_configuration()->policy().render_policy_id != policy.render_policy_id)
    throw std::invalid_argument("Worker render-color policy differs from captured Houdini configuration");
}
} // namespace cycles_houdini
