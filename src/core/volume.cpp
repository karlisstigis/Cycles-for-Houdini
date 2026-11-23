// SPDX-License-Identifier: Apache-2.0
#include "cycles_houdini/volume.h"
#include "scene/attribute.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <stdexcept>

namespace cycles_houdini {
namespace {
std::string canonical_field_name(const std::string &name) {
  if (name == "vel")
    return "velocity";
  for (int axis = 0; axis < 3; ++axis)
    if (name == std::string("vel.") + "xyz"[axis] || name == std::string("velocity.") + "xyz"[axis])
      return std::string("velocity_") + "xyz"[axis];
  return name;
}
bool vector_field_name(const std::string &name) {
  const auto standard = ccl::Attribute::name_standard(ccl::ustring(name));
  return !name.empty() && (standard == ccl::ATTR_STD_NONE || standard == ccl::ATTR_STD_VOLUME_COLOR ||
                           standard == ccl::ATTR_STD_VOLUME_VELOCITY);
}
} // namespace
VolumeVectorSources volume_vector_sources(const VolumeData &data, const std::string &name) {
  VolumeVectorSources result;
  const auto field = [&](const std::string &name) {
    return std::find_if(data.fields.begin(), data.fields.end(), [&](const auto &f) { return f.name == name; });
  };
  if (field(name) != data.fields.end()) {
    result.issue = "An explicit volume field already supplies " + name;
    return result;
  }
  bool any = false;
  std::array<std::string, 3> names;
  for (int axis = 0; axis < 3; ++axis) {
    names[axis] = name + "." + "xyz"[axis];
    any |= field(names[axis]) != data.fields.end();
  }
  if (!any) {
    result.issue = "Missing shader volume field: " + name;
    return result;
  }
  const auto problem = [&](const std::string &detail) {
    result.issue = "Cannot assemble vector volume field '" + name + "': " + detail;
  };
  if (!vector_field_name(name)) {
    problem("the name is reserved for a scalar or non-volume attribute");
    return result;
  }
  for (int axis = 0; axis < 3; ++axis) {
    const auto found = field(names[axis]);
    if (found == data.fields.end()) {
      problem("missing scalar component " + names[axis]);
      return result;
    }
    if (!found->grid || !found->grid->isType<openvdb::FloatGrid>()) {
      problem(names[axis] + " requires a scalar float grid");
      return result;
    }
    result.components[axis] = openvdb::gridConstPtrCast<openvdb::FloatGrid>(found->grid);
    result.revisions[axis] = found->revision;
    if (!found->grid->transform().isLinear() || found->grid->transform() != result.components[0]->transform()) {
      problem("component transforms/resolutions must match; resample explicitly in Houdini");
      return result;
    }
  }
  return result;
}
std::string volume_shader_field_error(const VolumeData &data, const std::string &name, bool scalar, bool vector) {
  const auto field =
      std::find_if(data.fields.begin(), data.fields.end(), [&](const auto &f) { return f.name == name; });
  if (field == data.fields.end()) {
    if (!scalar) {
      const auto source = volume_vector_sources(data, name);
      return source.usable() ? std::string{} : source.issue;
    }
    return "Missing shader volume field: " + name;
  }
  if (!field->grid)
    return "Shader volume field has no grid: " + name;
  if ((scalar && !field->grid->isType<openvdb::FloatGrid>()) || (vector && !field->grid->isType<openvdb::Vec3SGrid>()))
    return "Shader volume field has the wrong scalar/vector type: " + name;
  return {};
}
openvdb::Vec3SGrid::ConstPtr merge_volume_vector(const VolumeVectorSources &sources, const std::string &name) {
  if (!sources.usable() || !vector_field_name(name))
    throw std::invalid_argument("Vector volume assembly requires valid scalar sources and a vector-compatible name");
  for (const auto &source : sources.components)
    if (!source->transform().isLinear() || source->transform() != sources.components[0]->transform())
      throw std::invalid_argument("Vector volume component transforms must match");
  auto vector = openvdb::Vec3SGrid::create(openvdb::Vec3s(0));
  for (int axis = 0; axis < 3; ++axis) {
    auto merged = openvdb::Vec3SGrid::create(openvdb::Vec3s(0));
    const auto combine = [axis](openvdb::CombineArgs<openvdb::Vec3s, float> &args) {
      auto value = args.a();
      value[axis] = args.b();
      args.setResult(value);
      args.setResultIsActive(args.aIsActive() || args.bIsActive());
    };
    merged->tree().combine2Extended(vector->tree(), sources.components[axis]->tree(), combine);
    vector = std::move(merged);
  }
  vector->setName(name);
  vector->setTransform(sources.components[0]->transform().copy());
  vector->setVectorType(openvdb::VEC_INVARIANT);
  return vector;
}
std::string volume_field_error(const VolumeField &field) {
  if (field.name.empty())
    return "Unnamed sparse field";
  if (!field.grid)
    return "Sparse field has no grid: " + field.name;
  const bool scalar = field.grid->isType<openvdb::FloatGrid>(), vector = field.grid->isType<openvdb::Vec3SGrid>();
  if (!scalar && !vector)
    return "Unsupported sparse grid type for " + field.name + ": " + field.grid->type();
  if (!field.grid->transform().isLinear())
    return "Non-affine sparse grid transform: " + field.name;
  const auto name = canonical_field_name(field.name);
  const auto standard = ccl::Attribute::name_standard(ccl::ustring(name));
  const bool vector_field = standard == ccl::ATTR_STD_VOLUME_COLOR || standard == ccl::ATTR_STD_VOLUME_VELOCITY;
  const bool scalar_field = standard == ccl::ATTR_STD_VOLUME_DENSITY || standard == ccl::ATTR_STD_VOLUME_TEMPERATURE ||
                            standard == ccl::ATTR_STD_VOLUME_FLAME || standard == ccl::ATTR_STD_VOLUME_HEAT ||
                            standard == ccl::ATTR_STD_VOLUME_VELOCITY_X ||
                            standard == ccl::ATTR_STD_VOLUME_VELOCITY_Y || standard == ccl::ATTR_STD_VOLUME_VELOCITY_Z;
  if (standard != ccl::ATTR_STD_NONE && !(scalar_field || vector_field))
    return "Sparse field conflicts with a reserved non-volume attribute: " + field.name;
  if ((scalar_field && !scalar) || (vector_field && !vector))
    return "Sparse field requires a " + std::string(scalar_field ? "scalar float" : "vector float") +
           " grid: " + field.name;
  return {};
}
void normalize_volume_fields(VolumeData &data) {
  std::map<std::string, openvdb::GridBase::ConstPtr> fields;
  for (auto &field : data.fields) {
    field.name = canonical_field_name(field.name);
    if (!field.grid || !fields.emplace(field.name, field.grid).second)
      throw std::invalid_argument("Ambiguous or missing sparse field: " + field.name);
  }
  const std::array<std::string, 3> names = {"velocity_x", "velocity_y", "velocity_z"};
  int components = 0;
  for (const auto &name : names)
    components += fields.contains(name);
  if (!components)
    return;
  if (components != 3 || fields.contains("velocity"))
    throw std::invalid_argument(
        "Volume velocity requires one vector field or all three scalar components, without ambiguity");
  std::array<openvdb::FloatGrid::ConstPtr, 3> scalars;
  for (int axis = 0; axis < 3; ++axis) {
    if (!fields.at(names[axis])->isType<openvdb::FloatGrid>())
      throw std::invalid_argument("Volume velocity components must be float scalar grids");
    scalars[axis] = openvdb::gridConstPtrCast<openvdb::FloatGrid>(fields.at(names[axis]));
    if (scalars[axis]->transform() != scalars[0]->transform())
      throw std::invalid_argument("Volume velocity component transforms must match; resample explicitly in Houdini");
  }
  // Values are geometry-space vectors. The grid map locates samples; it must
  // not apply an additional vector rotation or translation to their values.
  auto velocity = merge_volume_vector(VolumeVectorSources{scalars, {}}, "velocity");
  std::erase_if(data.fields,
                [&](const auto &f) { return std::find(names.begin(), names.end(), f.name) != names.end(); });
  data.fields.push_back({"velocity", velocity});
}
std::array<float, 3> volume_velocity_bounds(const VolumeData &data) {
  if (!std::isfinite(data.velocity_seconds) || data.velocity_seconds < 0 ||
      !std::isfinite(data.velocity_reference_time) || data.velocity_reference_time < 0 ||
      data.velocity_reference_time > 1)
    throw std::invalid_argument("Volume motion requires finite nonnegative shutter seconds and a reference in [0,1]");
  std::array<float, 3> result{};
  if (data.velocity_seconds == 0)
    return result;
  const auto field =
      std::find_if(data.fields.begin(), data.fields.end(), [](const auto &f) { return f.name == "velocity"; });
  if (field == data.fields.end() || !field->grid || !field->grid->isType<openvdb::Vec3SGrid>())
    throw std::invalid_argument(
        "Volume velocity blur requires a vec3-float velocity/vel grid or three float components");
  const auto grid = openvdb::gridConstPtrCast<openvdb::Vec3SGrid>(field->grid);
  const double duration =
      double(data.velocity_seconds) * std::max(data.velocity_reference_time, 1.0f - data.velocity_reference_time);
  const auto accumulate = [&](const openvdb::Vec3s &value) {
    for (int axis = 0; axis < 3; ++axis) {
      const double distance = std::abs(double(value[axis])) * duration;
      if (!std::isfinite(value[axis]) || !std::isfinite(distance) || distance >= std::numeric_limits<float>::max())
        throw std::invalid_argument(
            "Volume velocity must contain finite values with a representable shutter displacement");
      // Round outward: this envelope must never underestimate an outlier.
      result[axis] = std::max(
          result[axis], distance > 0 ? std::nextafter(float(distance), std::numeric_limits<float>::infinity()) : 0.0f);
    }
  };
  accumulate(grid->background());
  for (auto it = grid->cbeginValueAll(); it; ++it)
    accumulate(*it);
  return result;
}
} // namespace cycles_houdini
