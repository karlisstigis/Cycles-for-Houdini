// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include "util/tbb.h"
#include <span>
#include <utility>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>

namespace cycles_houdini {
namespace {
// CYCLES_HOUDINI_HASH_CURVES=1 (equivalence tests only): after a curve update,
// print one CH_CURVE_HASH line to stderr (the worker log) with a hash of
// everything written into the engine's Hair node: curve_first_key,
// curve_shader, curve shape, motion settings and, for every
// attribute in its order (keys and radii are attributes in this engine, as
// are motion keys, intercept, length, Cd, uv, random, generated and named
// fields), its name, standard, type, element, flags, size and the bytes of
// every motion step. A three-float element stored in 16 bytes hashes only its
// first 12 (the fourth float is padding).
// Used shader names (material keys, per process) are printed apart, not in the total.
constexpr std::size_t strand_block = 4096;
template <class Body> void strand_blocks(std::size_t blocks, const Body &body) {
  const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  if (blocks <= 1 || (serial && std::strcmp(serial, "1") == 0)) {
    for (std::size_t block = 0; block < blocks; ++block)
      body(block);
    return;
  }
  tbb::parallel_for(std::size_t(0), blocks, [&](std::size_t block) { body(block); });
}
bool hash_curves_enabled() {
  static const bool enabled = [] {
    const char *value = std::getenv("CYCLES_HOUDINI_HASH_CURVES");
    return value && std::strcmp(value, "1") == 0;
  }();
  return enabled;
}
struct WordHash {
  std::uint64_t state = 0x9e3779b97f4a7c15ull;
  void word(std::uint64_t value) {
    state ^= value + 0x9e3779b97f4a7c15ull + (state << 6) + (state >> 2);
    state *= 0xff51afd7ed558ccdull;
    state ^= state >> 33;
  }
  void bytes(const void *data, std::size_t count) {
    const auto *p = static_cast<const unsigned char *>(data);
    std::size_t n = 0;
    for (; n + 8 <= count; n += 8) {
      std::uint64_t value;
      std::memcpy(&value, p + n, 8);
      word(value);
    }
    std::uint64_t tail = 0;
    if (n < count)
      std::memcpy(&tail, p + n, count - n);
    word(tail ^ (std::uint64_t(count) << 56));
  }
  void text(const std::string &value) { bytes(value.data(), value.size()); }
};
void hash_curves(const std::string &key, ccl::Hair *hair) {
  WordHash total;
  std::ostringstream parts;
  parts.imbue(std::locale::classic());
  const auto part = [&](const std::string &name, const WordHash &value) {
    total.text(name);
    total.word(value.state);
    parts << ",\"" << name << "\":\"" << std::hex << value.state << std::dec << '\"';
  };
  WordHash h;
  h.bytes(hair->get_curve_first_key().data(), hair->get_curve_first_key().size() * sizeof(int));
  part("first_key", h);
  h = {};
  h.bytes(hair->get_curve_shader().data(), hair->get_curve_shader().size() * sizeof(int));
  part("shader", h);
  h = {};
  h.word(std::uint64_t(hair->curve_shape));
  h.word(hair->get_motion_steps());
  h.word(hair->get_use_motion_blur());
  part("settings", h);
  parts << ",\"settings_values\":\"shape " << int(hair->curve_shape) << " steps " << hair->get_motion_steps()
        << " blur " << int(hair->get_use_motion_blur()) << '\"';
  // Shader names are material keys, which are per process: printed, not in the total.
  h = {};
  for (const auto *node : hair->get_used_shaders())
    h.text(node->name.string());
  parts << ",\"used_shaders\":\"" << std::hex << h.state << std::dec << '\"';
  for (const auto &attribute : hair->attributes.attributes) {
    h = {};
    h.text(attribute.name.string());
    h.word(std::uint64_t(attribute.std));
    h.text(attribute.type.c_str());
    h.word(std::uint64_t(attribute.element));
    h.word(attribute.flags);
    h.word(std::uint64_t(attribute.size));
    const auto element = attribute.data_sizeof();
    const bool padded = element == 16 && attribute.type.basetype == ccl::TypeDesc::FLOAT &&
                        attribute.type.aggregate == ccl::TypeDesc::VEC3;
    for (int step = 0; step < attribute.num_motion_steps(); ++step) {
      const auto *data = static_cast<const unsigned char *>(attribute.data(step));
      h.word(data != nullptr);
      if (!data)
        continue;
      if (padded)
        for (int n = 0; n < attribute.size; ++n)
          h.bytes(data + std::size_t(n) * 16, 12);
      else
        h.bytes(data, std::size_t(attribute.size) * element);
    }
    part("attribute:" + attribute.name.string(), h);
  }
  std::fprintf(stderr, "CH_CURVE_HASH {\"key\":\"%s\",\"curves\":%zu,\"keys\":%zu,\"hash\":\"%llx\"%s}\n", key.c_str(),
               hair->num_curves(), hair->num_keys(), (unsigned long long)total.state, parts.str().c_str());
  std::fflush(stderr);
}
} // namespace
void RenderSession::Impl::update_volume(std::shared_ptr<const VolumeData> data, const Float3 &motion_bounds,
                                        VolumeVectors vectors) {
  auto &record = meshes[data->key];
  if (record.node && record.kind != GeometryKind::Volume)
    throw std::invalid_argument("Use a distinct resource key when changing geometry type");
  auto *scene = session->scene.get();
  ccl::array<ccl::Node *> shaders;
  shaders.reserve(1);
  auto *shader = volume_recovery.contains(data->key) ? binding_materials.at({data->material, volume_fallback_binding})
                                                     : materials.at(data->material);
  // Cycles may skip an unreferenced shader during an earlier compile.
  // Reactivate it before the socket starts owning a reference to it.
  shader->tag_used(scene);
  shaders.push_back_reserved(shader);
  const bool same_vectors =
      vectors.size() == record.volume_vectors.size() &&
      std::equal(vectors.begin(), vectors.end(), record.volume_vectors.begin(),
                 [](const auto &a, const auto &b) { return a.first == b.first && a.second.grid == b.second.grid; });
  record.volume_vectors = std::move(vectors);
  record.volume_motion_bounds = motion_bounds;
  if (record.node && record.revision == data->revision &&
      record.materials == std::vector<std::string>{data->material} && same_vectors) {
    // An unchanged grid can switch between its source shader and a
    // recovered binding. Rebind without clearing/re-uploading voxels.
    if (record.node->get_used_shaders() != shaders) {
      record.node->set_used_shaders(shaders);
      record.node->tag_update(scene, false);
    }
    record.volume = std::move(data);
    return;
  }
  if (!record.node) {
    record.node = scene->create_node<ccl::Volume>();
    record.node->name = ccl::ustring(data->key);
    record.kind = GeometryKind::Volume;
  }
  auto *volume = static_cast<ccl::Volume *>(record.node);
  // Volume::clear preserves voxel attributes for Cycles' own bounds
  // rebuild. A source replacement must first retire old fields too;
  // otherwise removed/renamed grids retain images and stale bounds.
  volume->attributes.clear();
  volume->clear(true);
  volume->set_used_shaders(shaders);
  volume->set_object_space(data->object_space_density);
  volume->set_step_size(data->step_size);
  volume->set_velocity_scale(scene->integrator->get_motion_blur() ? data->velocity_seconds : 0);
  volume->set_velocity_time_offset(data->velocity_reference_time);
  volume->set_velocity_max_distance(vec(motion_bounds));
  record.bytes = 0;
  const auto add_field = [&](const VolumeField &field) {
    const ccl::ustring name(field.name);
    ccl::Attribute *attribute{};
    const auto standard = ccl::Attribute::name_standard(ccl::ustring(name));
    if (standard != ccl::ATTR_STD_NONE)
      attribute = volume->attributes.add(standard);
    else
      attribute = volume->attributes.add(
          name, field.grid->isType<openvdb::Vec3SGrid>() ? ccl::TypeVector : ccl::TypeFloat, ccl::ATTR_ELEMENT_VOXEL);
    ccl::ImageParams params;
    params.colorspace = ccl::ustring("Raw");
    // A density-style cutoff cannot be applied before the artist's
    // shader scale is known, and is inappropriate for other fields.
    // Zero also avoids the upstream clipping path's extra tree copy.
    attribute->data_voxel_for_write() =
        scene->image_manager->add_image(ccl::make_unique<ccl::VDBImageLoader>(field.grid, field.name, 0.0f), params);
    record.bytes += field.grid->memUsage();
  };
  for (const auto &field : data->fields)
    add_field(field);
  for (const auto &[name, vector] : record.volume_vectors)
    add_field({name, vector.grid});
  volume->merge_grids(scene);
  volume->tag_update(scene, true);
  record.revision = data->revision;
  record.materials = {data->material};
  record.volume = std::move(data);
  ++geometry_conversions;
}

void RenderSession::Impl::update_strand_generated(MeshRecord &record) {
  const bool needed = palette_generated(record.materials) && !record.strand_generated_fallback;
  if (needed == record.generated)
    return;
  if (needed) {
    auto *target = record.node->attributes.add(ccl::ATTR_STD_GENERATED)->data_for_write<ccl::packed_float3>();
    std::copy(record.strand_generated.begin(), record.strand_generated.end(), target);
  } else
    record.node->attributes.remove(ccl::ATTR_STD_GENERATED);
  const auto bytes = record.strand_generated.size() * sizeof(ccl::packed_float3);
  if (needed)
    record.bytes += bytes;
  else
    record.bytes -= bytes;
  record.generated = needed;
  record.node->tag_update(session->scene.get(), false);
}

void RenderSession::Impl::update_points(const PointData &data) {
  auto &record = meshes[data.key];
  if (record.node && record.kind != GeometryKind::Points)
    throw std::invalid_argument("Use a distinct resource key when changing geometry type");
  const auto palette = strand_materials(data);
  if (record.node && record.revision == data.revision && record.materials == palette &&
      record.gaussian_splats == bool(data.splats))
    return;
  if (!record.node) {
    record.node = session->scene->create_node<ccl::PointCloud>();
    record.node->name = ccl::ustring(data.key);
    record.kind = GeometryKind::Points;
  }
  auto *points = static_cast<ccl::PointCloud *>(record.node);
  points->clear(true);
  points->resize(int(data.positions.size()));
  std::fill(points->get_shader().begin(), points->get_shader().end(), 0);
  if (!data.material_slots.empty())
    std::copy(data.material_slots.begin(), data.material_slots.end(), points->get_shader().data());
  points->tag_shader_modified();
  update_point_attributes(points, data, false, palette);
  points->set_render_as(data.splats ? ccl::PointCloud::RENDER_AS_GSPLATS : ccl::PointCloud::RENDER_AS_POINTS);
  if (data.splats) {
    const auto &g = *data.splats;
    const auto upload = [&](auto standard, const auto &centre, const auto &samples, auto convert) {
      using Value = decltype(convert(centre.front()));
      auto *attr = points->attributes.add(standard);
      auto *target = attr->template data_for_write<Value>();
      for (const auto &v : centre)
        *target++ = convert(v);
      if (!samples.empty()) {
        attr->add_motion(points);
        for (std::size_t step = 0; step < samples.size(); ++step) {
          target = attr->template data_for_write<Value>(int(step + 1));
          for (const auto &v : samples[step])
            *target++ = convert(v);
        }
      }
    };
    upload(ccl::ATTR_STD_GSPLAT_SCALE, g.scales, g.motion_scales,
           [](const Float3 &v) { return ccl::packed_float3(ccl::make_float3(v.x, v.y, v.z)); });
    upload(ccl::ATTR_STD_GSPLAT_ROTATION, g.rotations, g.motion_rotations,
           [](const auto &v) { return ccl::make_quaternion(v[3], v[0], v[1], v[2]); });
    const auto to_rec709 = ccl::ColorSpaceManager::get_xyz_to_rec709() *
                           ccl::transform_inverse(ccl::ColorSpaceManager::get_xyz_to_scene_linear_rgb());
    upload(ccl::ATTR_STD_GSPLAT_RADIANCE_BASE, g.radiance, g.motion_radiance, [&](const auto &v) {
      if (!g.scene_linear_color)
        return ccl::make_float4(v[0], v[1], v[2], v[3]);
      const auto rgb = ccl::transform_direction(&to_rec709, ccl::make_float3(v[0], v[1], v[2]));
      const auto encode = [](float x) {
        return ((x <= 0.0031308f ? 12.92f * x : 1.055f * std::pow(x, 1.f / 2.4f) - .055f) - .5f) / .282094792f;
      };
      return ccl::make_float4(encode(rgb.x), encode(rgb.y), encode(rgb.z), v[3]);
    });
    if (!g.harmonics.empty()) {
      auto *target = points->attributes.add(ccl::ATTR_STD_GSPLAT_RADIANCE_SPHERICAL_HARMONICS_REST)
                         ->data_for_write<ccl::PackedSphericalHarmonicsRest>();
      for (const auto &coefficients : g.harmonics) {
        for (int i = 0; i < 15; ++i)
          ccl::spherical_harmonics_rest_set_coefficient(*target, i, vec(coefficients[i]));
        ++target;
      }
    }
    // The unbound upstream default reads the view-dependent radiance.
    ccl::array<ccl::Node *> shaders;
    shaders.reserve(palette.size());
    for (const auto &key : palette) {
      auto *shader = key.empty() ? session->scene->default_gsplat
                                 : binding_shader(key, !data.uv.empty(), !data.colors.empty(), true,
                                                  attribute_layout(data.attributes, data.instance_attributes),
                                                  data.instance_attributes);
      shader->tag_used(session->scene.get());
      shaders.push_back_reserved(shader);
    }
    points->set_used_shaders(shaders);
    if (!data.motion.positions.empty() && !points->attributes.find(ccl::ATTR_STD_RADIUS)->has_motion())
      points->attributes.find(ccl::ATTR_STD_RADIUS)->add_motion(points);
    points->update_gsplat_radii();
  }
  record.gaussian_splats = bool(data.splats);
  if (!data.lifetimes.empty()) {
    auto *values = points->attributes.add(ccl::ATTR_STD_POINT_LIFETIME)->data_for_write<ccl::float2>();
    for (const auto interval : data.lifetimes)
      *values++ = ccl::make_float2(interval.x, interval.y);
    points->tag_update(session->scene.get(), true);
  }
  record.revision = data.revision;
  record.materials = palette;
  record.attributes = attribute_layout(data.attributes, data.instance_attributes);
  record.instance_attributes = data.instance_attributes;
  record.has_uv = !data.uv.empty();
  record.has_color = !data.colors.empty();
  record.bytes = data.positions.size() * (sizeof(ccl::packed_float3) + sizeof(float) + sizeof(int)) +
                 data.colors.size() * sizeof(ccl::packed_float3) + data.randoms.size() * sizeof(float) +
                 (data.uv.size() + data.lifetimes.size()) * sizeof(ccl::float2) + geometry_motion_bytes(data.motion);
  if (data.splats) {
    const auto &g = *data.splats;
    record.bytes += g.scales.size() * sizeof(ccl::packed_float3) +
                    (g.rotations.size() + g.radiance.size()) * sizeof(ccl::float4) +
                    g.harmonics.size() * sizeof(ccl::PackedSphericalHarmonicsRest);
    for (const auto &sample : g.motion_scales)
      record.bytes += sample.size() * sizeof(ccl::packed_float3);
    for (const auto &sample : g.motion_rotations)
      record.bytes += sample.size() * sizeof(ccl::Quaternion);
    for (const auto &sample : g.motion_radiance)
      record.bytes += sample.size() * sizeof(ccl::float4);
  }
  record.strand_generated.clear();
  // Link point clouds: no Generated attribute, as Blender's translator (object-space position).
  record.strand_generated_fallback = !data.generated_attribute;
  if (data.generated_attribute) {
    record.strand_generated.reserve(data.positions.size());
    const GeneratedCoordinates coordinates(data.positions);
    for (const auto p : data.positions)
      record.strand_generated.emplace_back(vec(coordinates(p)));
  }
  record.bytes += named_attribute_bytes(data.attributes);
  for (const auto &sample : data.motion_colors)
    record.bytes += sample.size() * sizeof(ccl::packed_float3);
  for (const auto &sample : data.motion_uv)
    record.bytes += sample.size() * sizeof(ccl::float2);
  record.source_bytes = record.strand_generated.size() * sizeof(ccl::packed_float3);
  record.generated = false;
  update_strand_generated(record);
  ++geometry_conversions;
}

void RenderSession::Impl::update_curves(const CurveData &data) {
  auto &record = meshes[data.key];
  if (record.node && record.kind != GeometryKind::Curves)
    throw std::invalid_argument("Use a distinct resource key when changing geometry type");
  const auto palette = strand_materials(data);
  if (record.node && record.revision == data.revision && record.materials == palette)
    return;
  if (!record.node) {
    record.node = session->scene->create_node<ccl::Hair>();
    record.node->name = ccl::ustring(data.key);
    record.kind = GeometryKind::Curves;
  }
  auto *curves = static_cast<ccl::Hair *>(record.node);
  curves->clear(true);
  curves->resize_curves(int(data.first_key.size()), int(data.positions.size()));
  curves->curve_shape = data.shape == CurveShape::Linear  ? ccl::CURVE_THICK_LINEAR
                        : data.shape == CurveShape::Thick ? ccl::CURVE_THICK
                                                          : ccl::CURVE_RIBBON;
  std::copy(data.first_key.begin(), data.first_key.end(), curves->get_curve_first_key().data());
  std::fill(curves->get_curve_shader().begin(), curves->get_curve_shader().end(), 0);
  if (!data.material_slots.empty())
    std::copy(data.material_slots.begin(), data.material_slots.end(), curves->get_curve_shader().data());
  curves->tag_curve_first_key_modified();
  curves->tag_curve_shader_modified();
  update_point_attributes(curves, data, true, palette);
  auto *intercept = curves->attributes.add(ccl::ATTR_STD_CURVE_INTERCEPT)->data_for_write<float>();
  auto *length = curves->attributes.add(ccl::ATTR_STD_CURVE_LENGTH)->data_for_write<float>();
  record.strand_generated.clear();
  record.strand_generated.reserve(data.first_key.size());
  // Link curves follow Blender's translator: explicit per-curve values (legacy particle hair) or
  // no attribute at all (Curves objects; Cycles then uses the object-space position).
  record.strand_generated_fallback = data.generated.empty() && !data.generated_attribute;
  for (const auto &value : data.generated)
    record.strand_generated.emplace_back(vec(value));
  const bool bounds_generated = data.generated.empty() && data.generated_attribute;
  // Per-curve work on fixed blocks of curves (and of keys for the bounds),
  // parallel over the block index: every curve runs the same serial
  // arithmetic, so the values do not depend on the schedule.
  // CYCLES_HOUDINI_SERIAL_CONVERSION=1 runs the blocks on this thread.
  const std::size_t curve_count = data.first_key.size(), keys = data.positions.size();
  const Float3 *const positions = data.positions.data();
  const int *const first_keys = data.first_key.data();
  GeneratedCoordinates coordinates(std::span<const Float3>{});
  if (keys) {
    // Bounds of all keys: min/max of finite values is exact in any grouping.
    const std::size_t blocks = (keys + strand_block - 1) / strand_block;
    std::vector<std::pair<Float3, Float3>> block_bounds(blocks);
    strand_blocks(blocks, [&](std::size_t block) {
      const auto begin = block * strand_block, end = std::min(keys, begin + strand_block);
      auto low = positions[begin], high = low;
      for (auto i = begin; i < end; ++i) {
        const auto p = positions[i];
        low = {std::min(low.x, p.x), std::min(low.y, p.y), std::min(low.z, p.z)};
        high = {std::max(high.x, p.x), std::max(high.y, p.y), std::max(high.z, p.z)};
      }
      block_bounds[block] = {low, high};
    });
    auto low = positions[0], high = low;
    for (const auto &[l, h] : block_bounds) {
      low = {std::min(low.x, l.x), std::min(low.y, l.y), std::min(low.z, l.z)};
      high = {std::max(high.x, h.x), std::max(high.y, h.y), std::max(high.z, h.z)};
    }
    coordinates.low = low;
    coordinates.extent = {high.x - low.x, high.y - low.y, high.z - low.z};
  }
  if (bounds_generated)
    record.strand_generated.resize(curve_count);
  ccl::packed_float3 *const generated_out = bounds_generated ? record.strand_generated.data() : nullptr;
  strand_blocks((curve_count + strand_block - 1) / strand_block, [&](std::size_t block) {
    const auto first = block * strand_block, last = std::min(curve_count, first + strand_block);
    for (auto curve = first; curve < last; ++curve) {
      const auto start = std::size_t(first_keys[curve]),
                 end = curve + 1 < curve_count ? std::size_t(first_keys[curve + 1]) : keys;
      float total = 0;
      intercept[start] = 0;
      for (auto key = start + 1; key < end; ++key) {
        const auto a = positions[key - 1], b = positions[key];
        total += std::sqrt((a.x - b.x) * (a.x - b.x) + (a.y - b.y) * (a.y - b.y) + (a.z - b.z) * (a.z - b.z));
        intercept[key] = total;
      }
      length[curve] = total;
      for (auto key = start; key < end; ++key)
        intercept[key] = total > 0 ? intercept[key] / total : 0;
      if (generated_out)
        generated_out[curve] = ccl::packed_float3(vec(coordinates(positions[start])));
    }
  });
  record.revision = data.revision;
  record.materials = palette;
  record.attributes = attribute_layout(data.attributes, data.instance_attributes);
  record.instance_attributes = data.instance_attributes;
  record.has_uv = !data.uv.empty();
  record.has_color = !data.colors.empty();
  record.bytes = data.positions.size() * (sizeof(ccl::packed_float3) + sizeof(float)) +
                 data.first_key.size() * sizeof(int) * 2 + data.colors.size() * sizeof(ccl::packed_float3) +
                 data.randoms.size() * sizeof(float) + data.uv.size() * sizeof(ccl::float2) +
                 geometry_motion_bytes(data.motion);
  record.bytes += (data.positions.size() + data.first_key.size()) * sizeof(float);
  record.bytes += named_attribute_bytes(data.attributes);
  for (const auto &sample : data.motion_colors)
    record.bytes += sample.size() * sizeof(ccl::packed_float3);
  for (const auto &sample : data.motion_uv)
    record.bytes += sample.size() * sizeof(ccl::float2);
  record.source_bytes = record.strand_generated.size() * sizeof(ccl::packed_float3);
  record.generated = false;
  update_strand_generated(record);
  if (hash_curves_enabled())
    hash_curves(data.key, curves);
  ++geometry_conversions;
}

} // namespace cycles_houdini
