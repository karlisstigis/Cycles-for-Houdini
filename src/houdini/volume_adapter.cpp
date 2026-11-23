// SPDX-License-Identifier: Apache-2.0
#include "volume_adapter.h"
#include "scene_adapter.h"
#include "cycles_houdini/volume.h"
#include <algorithm>
#include <stdexcept>
#include <set>
#include <map>
#include <bit>
#include <cmath>
#include <variant>
#include <type_traits>
#include <tuple>
#include <atomic>
#include <limits>
#include <sstream>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GEO/GEO_PrimVolume.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimVDB.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini {
namespace {
std::uint64_t next_field_revision() {
  static std::atomic<std::uint64_t> next{1};
  auto value = next.load(std::memory_order_relaxed);
  do {
    if (value == std::numeric_limits<std::uint64_t>::max())
      throw std::overflow_error("Volume field identity space exhausted");
  } while (!next.compare_exchange_weak(value, value + 1, std::memory_order_relaxed));
  return value;
}
using DenseVoxels = std::variant<UT_VoxelArrayReadHandleF, UT_VoxelArrayReadHandleV3, UT_VoxelArrayReadHandleI>;
struct DenseField {
  std::size_t index{};
  DenseVoxels voxels;
  openvdb::Mat4d index_to_object{openvdb::Mat4d::identity()};
  // Value-only SDK map: no primitive/detail pointer survives host capture.
  GEO_PrimVolumeXform mapping;
  std::array<float, 2> taper{1, 1};
};
struct DenseResult {
  openvdb::GridBase::ConstPtr grid;
  std::string error, error_code, notice;
};
std::string dense_transform_error(const GEO_PrimVolume &dense, openvdb::Mat4d *result = nullptr) {
  int x, y, z;
  dense.getRes(x, y, z);
  const auto type = dense.getStorageType();
  if (x <= 0 || y <= 0 || z <= 0)
    return "Dense volume has no usable voxel dimensions";
  if (!std::isfinite(dense.getTaperX()) || !std::isfinite(dense.getTaperY()))
    return "Dense volume taper contains non-finite values";
  if (type != GEO_PrimVolume::VOLUME_TYPE_FLOAT && type != GEO_PrimVolume::VOLUME_TYPE_VECTOR3 &&
      type != GEO_PrimVolume::VOLUME_TYPE_INTEGER)
    return "Unsupported dense volume storage: " + std::to_string(int(type));
  UT_DMatrix4 transform;
  dense.getTransform4(transform);
  const double res[3] = {double(x), double(y), double(z)};
  auto m = openvdb::Mat4d::identity();
  for (int col = 0; col < 3; ++col) {
    m[3][col] = transform(3, col);
    for (int row = 0; row < 3; ++row) {
      m[row][col] = 2 / res[row] * transform(row, col);
      m[3][col] += (-1 + 1 / res[row]) * transform(row, col);
    }
  }
  const double determinant = m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) -
                             m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
                             m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
  bool usable = std::isfinite(determinant) && determinant != 0;
  for (int row = 0; row < 4; ++row)
    for (int col = 0; col < 4; ++col)
      usable &= std::isfinite(float(m[row][col]));
  if (!usable)
    return "Dense volume has a non-finite or singular transform";
  const auto inverse = m.inverse();
  for (int row = 0; row < 4; ++row)
    for (int col = 0; col < 4; ++col)
      usable &= std::isfinite(float(inverse[row][col]));
  if (!usable)
    return "Dense volume transform exceeds the renderer numeric range";
  if (result)
    *result = m;
  return {};
}

template <class Handle>
DenseResult dense_grid(const Handle &handle, const openvdb::Mat4d &matrix, const std::string &name) {
  if (handle.isNull())
    return {{}, "Dense volume has no voxel data"};
  using Value = std::decay_t<decltype(handle->getValue(0, 0, 0))>;
  constexpr bool vector = std::is_same_v<Value, UT_Vector3>;
  using Grid = std::conditional_t<vector, openvdb::Vec3SGrid, openvdb::FloatGrid>;
  using Target = typename Grid::ValueType;
  const auto convert = [](const Value &value) -> Target {
    if constexpr (vector)
      return openvdb::Vec3s(float(value[0]), float(value[1]), float(value[2]));
    else
      return float(value);
  };
  const auto finite = [](const Target &value) {
    if constexpr (vector)
      return std::isfinite(value[0]) && std::isfinite(value[1]) && std::isfinite(value[2]);
    else
      return std::isfinite(value);
  };
  auto grid = Grid::create(Target(0));
  auto accessor = grid->getAccessor();
  // Honor compressed constant tiles without expanding the full dense array.
  // Nonconstant tiles are read in their native tile-local order.
  for (int tile = 0; tile < handle->numTiles(); ++tile) {
    UT_Vector3I start, end;
    handle->getTileVoxels(tile, start, end);
    if (handle->getLinearTile(tile)->isConstant()) {
      const auto value = convert(handle->getValue(start.x(), start.y(), start.z()));
      if (!finite(value))
        return {{}, "Dense volume contains non-finite voxel values"};
      if (value != Target(0)) {
        grid->tree().fill(openvdb::CoordBBox(openvdb::Coord(start.x(), start.y(), start.z()),
                                             openvdb::Coord(end.x() - 1, end.y() - 1, end.z() - 1)),
                          value, true);
        accessor.clear();
      }
    } else {
      for (int z = start.z(); z < end.z(); ++z)
        for (int y = start.y(); y < end.y(); ++y)
          for (int x = start.x(); x < end.x(); ++x) {
            const auto value = convert(handle->getValue(x, y, z));
            if (!finite(value))
              return {{}, "Dense volume contains non-finite voxel values"};
            if (value != Target(0))
              accessor.setValueOn(openvdb::Coord(x, y, z), value);
          }
    }
  }
  grid->setTransform(openvdb::math::Transform::createLinearTransform(matrix));
  grid->setName(name);
  if constexpr (vector)
    grid->setVectorType(openvdb::VEC_INVARIANT);
  else
    grid->setGridClass(openvdb::GRID_FOG_VOLUME);
  return {grid, {}};
}

// Finite resampling is an approximation of a nonlinear field, not a new
// renderer representation. Retain the primitive's oriented affine lattice so
// rotation/shear cannot inflate a world-axis bounding box. Positive tapers use
// at least twice the sampling at the narrowest end. A collapsed apex/interior
// plane has zero measure: use twice nominal sampling and skip that plane.
// There is no silent quality reduction when this explicit plan is too large.
constexpr std::uint64_t max_resampled_voxels = 64ull * 1024 * 1024;
constexpr int max_resampled_axis = 2048;
template <class Handle>
DenseResult resampled_dense_grid(const Handle &handle, const DenseField &field, const std::string &name) {
  if (handle.isNull())
    return {{}, "Dense volume has no voxel data"};
  using Value = std::decay_t<decltype(handle->getValue(0, 0, 0))>;
  constexpr bool vector = std::is_same_v<Value, UT_Vector3>;
  using Grid = std::conditional_t<vector, openvdb::Vec3SGrid, openvdb::FloatGrid>;
  using Target = typename Grid::ValueType;
  const auto convert = [](const Value &value) -> Target {
    if constexpr (vector)
      return openvdb::Vec3s(float(value[0]), float(value[1]), float(value[2]));
    else
      return float(value);
  };
  const auto finite = [](const Target &value) {
    if constexpr (vector)
      return std::isfinite(value[0]) && std::isfinite(value[1]) && std::isfinite(value[2]);
    else
      return std::isfinite(value);
  };
  const std::array<int, 3> source = {handle->getXRes(), handle->getYRes(), handle->getZRes()};
  const std::array<double, 3> extent = {std::max(1., std::abs(double(field.taper[0]))),
                                        std::max(1., std::abs(double(field.taper[1]))), 1.};
  std::array<int, 3> target;
  std::uint64_t count = 1;
  const auto budget_error = [&](const std::string &reason) -> DenseResult {
    return {{},
            "Tapered field '" + name + "' cannot be resampled at its required resolution: " + reason + "; source " +
                std::to_string(source[0]) + "x" + std::to_string(source[1]) + "x" + std::to_string(source[2]) +
                "; limits 2048 per axis and 67108864 target voxels; no coarser field was substituted",
            "volume.resample_budget"};
  };
  for (int axis = 0; axis < 3; ++axis) {
    const double taper = axis < 2 ? double(field.taper[axis]) : 1.;
    const double narrow = taper > 0 ? std::min(1., taper) : 1.;
    const double required = std::ceil(2. * source[axis] * extent[axis] / narrow);
    if (!std::isfinite(required) || required < 1 || required > max_resampled_axis)
      return budget_error("axis " + std::to_string(axis) + " needs " + std::to_string(required) + " samples");
    target[axis] = int(required);
    count *= std::uint64_t(target[axis]);
  }
  if (count > max_resampled_voxels)
    return budget_error(std::to_string(count) + " target voxels required");
  // Validate the complete frozen source, including voxels not reached by the
  // target lattice. Do not let NaNs hide in a thin frustum region or a tile.
  for (int tile = 0; tile < handle->numTiles(); ++tile) {
    UT_Vector3I start, end;
    handle->getTileVoxels(tile, start, end);
    if (handle->getLinearTile(tile)->isConstant()) {
      if (!finite(convert(handle->getValue(start.x(), start.y(), start.z()))))
        return {{}, "Dense volume contains non-finite voxel values"};
    } else
      for (int z = start.z(); z < end.z(); ++z)
        for (int y = start.y(); y < end.y(); ++y)
          for (int x = start.x(); x < end.x(); ++x)
            if (!finite(convert(handle->getValue(x, y, z))))
              return {{}, "Dense volume contains non-finite voxel values"};
  }
  auto matrix = openvdb::Mat4d::identity();
  for (int col = 0; col < 3; ++col) {
    matrix[3][col] = field.mapping.myCenter[col];
    for (int row = 0; row < 3; ++row) {
      matrix[row][col] = 2. * extent[row] / target[row] * field.mapping.myXform(row, col);
      matrix[3][col] += (-extent[row] + extent[row] / target[row]) * field.mapping.myXform(row, col);
    }
  }
  const auto inverse = matrix.inverse();
  for (int row = 0; row < 4; ++row)
    for (int col = 0; col < 4; ++col)
      if (!std::isfinite(float(matrix[row][col])) || !std::isfinite(float(inverse[row][col])))
        return {{}, "Resampled dense volume transform exceeds the renderer numeric range"};
  auto mapping = field.mapping;
  mapping.myXform.identity();
  mapping.myInverseXform.identity();
  mapping.myCenter.assign(0, 0, 0);
  auto grid = Grid::create(Target(0));
  auto accessor = grid->getAccessor();
  for (int z = 0; z < target[2]; ++z)
    for (int y = 0; y < target[1]; ++y)
      for (int x = 0; x < target[0]; ++x) {
        const UT_Vector3 position(float(-extent[0] + (x + .5) * 2. * extent[0] / target[0]),
                                  float(-extent[1] + (y + .5) * 2. * extent[1] / target[1]),
                                  float(-1. + (z + .5) * 2. / target[2]));
        // The SDK defines a collapsed coordinate as zero in its inverse. Do
        // not turn that convention into density across an entire affine plane.
        const fpreal fraction = (1 - position.z()) * .5f;
        if (1 + (-1 + mapping.myTaperX) * fraction == 0 || 1 + (-1 + mapping.myTaperY) * fraction == 0)
          continue;
        const auto unit = mapping.toVoxelSpace(position);
        // Native voxel arrays commonly streak beyond their boundary. That is
        // a sampling condition, not infinite physical density: clip the true
        // warped support before sampling instead of filling the affine AABB.
        if (unit.x() < 0 || unit.x() > 1 || unit.y() < 0 || unit.y() > 1 || unit.z() < 0 || unit.z() > 1)
          continue;
        if (!std::isfinite(unit.x()) || !std::isfinite(unit.y()) || !std::isfinite(unit.z()))
          return {{}, "Tapered dense volume inverse map produced non-finite coordinates"};
        const auto value = convert((*handle)(unit));
        if (!finite(value))
          return {{}, "Tapered dense volume interpolation exceeds finite renderer precision"};
        if (value != Target(0))
          accessor.setValueOn(openvdb::Coord(x, y, z), value);
      }
  grid->setTransform(openvdb::math::Transform::createLinearTransform(matrix));
  grid->setName(name);
  if constexpr (vector)
    grid->setVectorType(openvdb::VEC_INVARIANT);
  else
    grid->setGridClass(openvdb::GRID_FOG_VOLUME);
  const auto dimensions = [](const std::array<int, 3> &value) {
    return std::to_string(value[0]) + "x" + std::to_string(value[1]) + "x" + std::to_string(value[2]);
  };
  const std::string notice =
      "Resampled native tapered field '" + name + "' from " + dimensions(source) + " to " + dimensions(target) +
      " on its oriented affine lattice (linear finite-grid approximation" +
      (field.taper[0] <= 0 || field.taper[1] <= 0 ? std::string("; collapsed apex/plane uses twice nominal sampling")
                                                  : std::string("; twice minimum-end sampling")) +
      "; " + std::to_string(count) + " target voxels)";
  grid->insertMeta("cycles_houdini:resampling", openvdb::StringMetadata(notice));
  return {grid, {}, {}, notice};
}
} // namespace
struct PreparedVolume {
  VolumeData data;
  std::vector<std::int64_t> identifiers;
  std::vector<DenseField> dense;
  std::vector<std::string> errors;
  // Effective bindings follow original SOP field order. Choose the material
  // only after voxel conversion establishes which density is actually usable.
  std::vector<std::string> field_materials;
  std::map<std::string, std::string> material_paths;
};
struct VolumeConversionCache {
  using Key = std::tuple<std::uintptr_t, std::size_t, std::array<double, 16>, std::array<float, 2>, std::string>;
  struct Entry {
    DenseVoxels source;
    DenseResult result;
  };
  std::map<Key, Entry> fields;
  using FieldKey = std::tuple<std::uintptr_t, std::string, std::string, std::string>;
  struct FieldIdentity {
    openvdb::GridBase::ConstPtr owner;
    std::uint64_t revision{};
  };
  std::map<FieldKey, FieldIdentity> field_identities;
};
std::shared_ptr<VolumeConversionCache> make_volume_conversion_cache() {
  return std::make_shared<VolumeConversionCache>();
}
std::string native_volume_name(const GU_Detail &source, const GA_Primitive &primitive) {
  if (const auto *vdb = dynamic_cast<const GU_PrimVDB *>(&primitive))
    return vdb->getGridName() ? vdb->getGridName() : "";
  const auto *attribute = source.findPrimitiveAttribute("name");
  GA_ROHandleS name(attribute);
  return name.isValid() && attribute->getTupleSize() == 1 ? name.get(primitive.getMapOffset()).toStdString() : "";
}
const std::vector<std::int64_t> &volume_capture_identifiers(const PreparedVolume &prepared) {
  return prepared.identifiers;
}
std::size_t dense_volume_fields(const PreparedVolume &prepared) { return prepared.dense.size(); }
std::vector<std::pair<std::uintptr_t, std::uint64_t>> volume_source_allocations(const PreparedVolume &prepared) {
  std::map<std::uintptr_t, std::uint64_t> result;
  for (const auto &field : prepared.dense)
    std::visit(
        [&](const auto &handle) {
          if (!handle.isNull())
            result.try_emplace(reinterpret_cast<std::uintptr_t>(handle.get()), handle->getMemoryUsage(true));
        },
        field.voxels);
  return {result.begin(), result.end()};
}

std::shared_ptr<const PreparedVolume> prepare_volume(const GU_Detail &source, const GeometryRequest &request,
                                                     const NativeGeometrySelection &selection, const std::string &key,
                                                     std::uint64_t revision, const NativeMotionWindow &window) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Volume preparation requires the host thread");
  if (selection.kind != NativeGeometryKind::Volume)
    throw std::invalid_argument("Volume preparation requires a volume selection");
  if (window.enabled && request.motion == NativeGeometryMotion::Deformation)
    throw std::invalid_argument(
        "Volume deformation sampling requires a field interpolation method; use velocity blur: " + request.sop_path);
  auto result = std::make_shared<PreparedVolume>();
  auto &data = result->data;
  data.key = key;
  data.revision = revision;
  data.material = request.material_key;
  data.step_size = request.volume_step_size;
  data.object_space_density = request.volume_object_space;
  data.recover_material = true;
  auto &identifiers = result->identifiers;
  identifiers = {std::bit_cast<std::int32_t>(request.volume_step_size), request.volume_object_space};
  const auto append_string = [&](const std::string &value) {
    identifiers.push_back(value.size());
    for (unsigned char c : value)
      identifiers.push_back(c);
  };
  NativeMaterialBindings bindings(source, request, &selection);
  std::vector<int> material_slots;
  for (GA_Iterator it(selection.primitive_range(source)); !it.atEnd(); ++it) {
    const auto *primitive = dynamic_cast<const GU_PrimVDB *>(source.getPrimitive(*it));
    const auto *dense = dynamic_cast<const GEO_PrimVolume *>(source.getPrimitive(*it));
    if (!primitive && !dense)
      throw std::logic_error("Volume selection contains a non-volume primitive: " + request.sop_path);
    const auto name = native_volume_name(source, *source.getPrimitive(*it));
    append_string(name);
    material_slots.push_back(bindings.primitive(*it));
    const auto index = data.fields.size();
    result->errors.emplace_back();
    if (dense) {
      identifiers.push_back(1);
      const auto type = dense->getStorageType();
      identifiers.push_back(type);
      data.fields.push_back({name, {}});
      int x, y, z;
      dense->getRes(x, y, z);
      identifiers.insert(identifiers.end(), {x, y, z});
      UT_DMatrix4 transform;
      dense->getTransform4(transform);
      for (int row = 0; row < 4; ++row)
        for (int col = 0; col < 4; ++col)
          identifiers.push_back(std::bit_cast<std::int64_t>(double(transform(row, col))));
      identifiers.push_back(std::bit_cast<std::int32_t>(dense->getTaperX()));
      identifiers.push_back(std::bit_cast<std::int32_t>(dense->getTaperY()));
      DenseField field;
      field.index = index;
      field.mapping = dense->getSpaceTransform();
      field.taper = {dense->getTaperX(), dense->getTaperY()};
      result->errors.back() = dense_transform_error(*dense, &field.index_to_object);
      if (!result->errors.back().empty())
        continue;
      // Documented GPU-cache synchronization belongs to host capture.
      // Voxel handles alone do not publish pending OpenCL writes.
      const_cast<GEO_PrimVolume *>(dense)->flushCEWriteCaches();
      if (type == GEO_PrimVolume::VOLUME_TYPE_VECTOR3)
        field.voxels = UT_VoxelArrayReadHandleV3(dense->getVoxelHandleV3());
      else if (type == GEO_PrimVolume::VOLUME_TYPE_INTEGER)
        field.voxels = UT_VoxelArrayReadHandleI(dense->getVoxelHandleI());
      else
        field.voxels = UT_VoxelArrayReadHandleF(dense->getVoxelHandleF());
      std::visit(
          [&](const auto &handle) {
            identifiers.push_back(std::int64_t(reinterpret_cast<std::uintptr_t>(handle.get())));
          },
          field.voxels);
      result->dense.push_back(std::move(field));
      continue;
    }
    identifiers.push_back(0);
    identifiers.push_back(primitive->hasGrid() ? 1 : 0);
    identifiers.insert(identifiers.end(),
                       {std::int64_t(primitive->getTreeUniqueId()), std::int64_t(primitive->getMetadataUniqueId()),
                        std::int64_t(primitive->getTransformUniqueId())});
    const auto anchor = source.getPos3(primitive->getPointOffset(0));
    for (int axis = 0; axis < 3; ++axis)
      identifiers.push_back(std::bit_cast<std::int64_t>(double(anchor(axis))));
    // Keep a missing header as a source field so conversion can report it
    // locally without discarding the other fields or geometry components.
    if (!primitive->hasGrid()) {
      data.fields.push_back({name, {}});
      continue;
    }
    // getGridPtr can synchronize the primitive's point translation even
    // through a const accessor. Do it here, never on a conversion thread.
    const auto grid = primitive->getGridPtr();
    // copyGrid alone shares its transform. A new transform and deep-copied
    // metadata isolate subsequent anchor/name edits; a distinct grid also
    // holds a distinct tree reference for GEO_PrimVDB::makeGridUnique().
    data.fields.push_back({name, grid ? grid->copyGridReplacingTransform(grid->transform().copy()) : nullptr});
  }
  const auto materials = bindings.materials();
  for (const auto slot : material_slots) {
    result->field_materials.push_back(materials.at(slot));
    append_string(result->field_materials.back());
  }
  for (const auto &reference : bindings.references())
    result->material_paths.emplace(reference.key, reference.path);
  if (window.enabled && attribute_motion(request.motion)) {
    const double duration = window.deltas.back() - window.deltas.front();
    data.velocity_seconds = float(duration);
    data.velocity_reference_time = float(-window.deltas.front() / duration);
  }
  identifiers.push_back(data.velocity_seconds > 0);
  if (data.velocity_seconds > 0) {
    identifiers.push_back(std::bit_cast<std::int64_t>(window.deltas.front()));
    identifiers.push_back(std::bit_cast<std::int64_t>(window.deltas.back()));
  }
  return result;
}
std::shared_ptr<const VolumeData> convert_volume(const PreparedVolume &prepared, std::uint64_t revision,
                                                 std::string *motion_warning,
                                                 std::vector<SourceDiagnostic> *diagnostics,
                                                 VolumeConversionCache *cache, std::uint64_t *dense_conversions) {
  if (motion_warning)
    motion_warning->clear();
  if (dense_conversions)
    *dense_conversions = 0;
  auto data = std::make_shared<VolumeData>(prepared.data);
  auto errors = prepared.errors;
  std::vector<std::string> error_codes(errors.size()), notices(errors.size());
  std::map<VolumeConversionCache::Key, VolumeConversionCache::Entry> retained_fields;
  for (const auto &field : prepared.dense) {
    std::array<double, 16> matrix;
    for (int row = 0; row < 4; ++row)
      for (int col = 0; col < 4; ++col)
        matrix[row * 4 + col] = field.index_to_object[row][col];
    const auto pointer =
        std::visit([](const auto &handle) { return reinterpret_cast<std::uintptr_t>(handle.get()); }, field.voxels);
    const VolumeConversionCache::Key key{pointer, field.voxels.index(), matrix, field.taper,
                                         data->fields.at(field.index).name};
    DenseResult converted;
    if (cache && cache->fields.contains(key))
      converted = cache->fields.at(key).result;
    else {
      converted = std::visit(
          [&](const auto &handle) {
            return field.mapping.isTapered()
                       ? resampled_dense_grid(handle, field, data->fields.at(field.index).name)
                       : dense_grid(handle, field.index_to_object, data->fields.at(field.index).name);
          },
          field.voxels);
      if (dense_conversions)
        ++*dense_conversions;
    }
    retained_fields.try_emplace(key, VolumeConversionCache::Entry{field.voxels, converted});
    data->fields.at(field.index).grid = converted.grid;
    errors.at(field.index) = converted.error;
    error_codes.at(field.index) = converted.error_code;
    notices.at(field.index) = converted.notice;
  }
  if (cache)
    cache->fields = std::move(retained_fields);
  if (cache) {
    std::map<VolumeConversionCache::FieldKey, VolumeConversionCache::FieldIdentity> identities;
    for (auto &field : data->fields)
      if (field.grid) {
        // Frozen grid owners protect tree identity from in-place writes
        // and address reuse. Header comparison includes transforms and
        // rendering metadata, without scanning or copying voxel values.
        std::ostringstream metadata;
        field.grid->writeMeta(metadata);
        field.grid->transform().write(metadata);
        const VolumeConversionCache::FieldKey key{reinterpret_cast<std::uintptr_t>(field.grid->baseTreePtr().get()),
                                                  field.name, field.grid->type(), metadata.str()};
        const auto previous = cache->field_identities.find(key);
        field.revision = previous == cache->field_identities.end() ? next_field_revision() : previous->second.revision;
        identities.emplace(key, VolumeConversionCache::FieldIdentity{field.grid, field.revision});
      }
    cache->field_identities = std::move(identities);
  }
  if (revision)
    data->revision = revision;
  if (diagnostics) {
    diagnostics->clear();
    std::set<std::string> names;
    std::size_t index = 0;
    std::erase_if(data->fields, [&](const auto &field) {
      const auto original = index++;
      std::string reason = errors.at(original);
      if (reason.empty())
        reason = volume_field_error(field);
      else
        reason = "Dense field '" + field.name + "': " + reason;
      // Select the first usable field in SOP primitive order. An invalid
      // earlier grid must not mask a later usable grid of the same name.
      if (reason.empty() && !names.insert(field.name).second)
        reason = "Duplicate sparse field: " + field.name;
      if (reason.empty()) {
        if (field.name == "density")
          data->material = prepared.field_materials.at(original);
        if (!notices.at(original).empty())
          diagnostics->push_back({"volume.resampled", "", data->key, notices.at(original),
                                  "Rendering the bounded affine resampling; source geometry remains unchanged", "info",
                                  field.name});
        return false;
      }
      diagnostics->push_back({error_codes.at(original).empty() ? "volume.field_ignored" : error_codes.at(original), "",
                              data->key, reason, "Omitting this field; retaining the first usable field with each name",
                              "warning", field.name});
      return true;
    });
    if (!names.contains("density")) {
      diagnostics->push_back({"volume.density_unavailable", "", data->key, "No usable scalar density field",
                              "Omitting this sparse volume component; other geometry continues rendering"});
      return {};
    }
    const std::set<std::string> materials(prepared.field_materials.begin(), prepared.field_materials.end());
    if (materials.size() > 1) {
      const auto path = [&](const std::string &key) {
        const auto found = prepared.material_paths.find(key);
        return found == prepared.material_paths.end() ? std::string("(default material)") : found->second;
      };
      std::string ignored;
      for (const auto &material : materials)
        if (material != data->material) {
          if (!ignored.empty())
            ignored += ", ";
          ignored += path(material);
        }
      diagnostics->push_back({"volume.conflicting_materials", "", data->key,
                              "Volume fields have conflicting materials; density assignment: " + path(data->material) +
                                  "; ignored: " + ignored,
                              "Using the first usable scalar density field's effective material for this volume"});
    }
  } else
    for (const auto &error : errors)
      if (!error.empty())
        throw std::invalid_argument(error);
  const auto retained = *data;
  // The private grid headers keep tree references alive independently of
  // their Houdini primitives. Supported host writes make those trees unique;
  // renderer/transport consumers only read them. Do not duplicate every tree
  // again. Normalization allocates new storage only for scalar velocity merges.
  try {
    normalize_volume_fields(*data);
    if (std::none_of(data->fields.begin(), data->fields.end(),
                     [](const auto &field) { return field.name == "velocity"; }))
      data->velocity_seconds = 0;
    volume_velocity_bounds(*data);
  } catch (const std::invalid_argument &error) {
    // Retry from the original headers, removing only motion fields. An
    // unrelated error (e.g. duplicate density) must still fail validation.
    *data = retained;
    const auto removed = std::erase_if(data->fields, [](const auto &field) {
      const auto &n = field.name;
      return n == "vel" || n == "velocity" || n == "vel.x" || n == "vel.y" || n == "vel.z" || n == "velocity.x" ||
             n == "velocity.y" || n == "velocity.z" || n == "velocity_x" || n == "velocity_y" || n == "velocity_z";
    });
    if (!removed)
      throw;
    data->velocity_seconds = 0;
    normalize_volume_fields(*data);
    if (motion_warning)
      *motion_warning = std::string("velocity blur disabled; ") + error.what() + ". Rendering current volume.";
  }
  return data;
}
} // namespace cycles_houdini
