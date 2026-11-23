// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/volume.h"
#include <openvdb/io/File.h>
#include <filesystem>
#include <limits>

namespace cycles_houdini::harness {
// Independently specified fixture, not an export from the Houdini adapter:
// nine voxel centres on each axis, spacing 1/8, density 1, zero background.
// The centre ray's integrated trilinear density is 9/8 in grid/world units.
inline openvdb::FloatGrid::Ptr density_grid() {
  openvdb::initialize();
  auto grid = openvdb::FloatGrid::create(0.0f);
  grid->setName("density");
  grid->setGridClass(openvdb::GRID_FOG_VOLUME);
  grid->setTransform(openvdb::math::Transform::createLinearTransform(0.125));
  grid->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 1.0f, true);
  return grid;
}
inline void write_density_fixture(const std::string &filename) {
  openvdb::io::File file(filename);
  file.write(openvdb::GridPtrVec{density_grid()});
  file.close();
}
inline void write_pyro_fixture(const std::string &filename) {
  auto density = density_grid();
  auto temperature = density->deepCopy();
  temperature->setName("temperature");
  temperature->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 2.0f, true);
  auto emission = density->deepCopy();
  emission->setName("emission");
  emission->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 0.25f, true);
  auto velocity = openvdb::Vec3SGrid::create(openvdb::Vec3s(0));
  velocity->setName("velocity");
  velocity->setTransform(openvdb::math::Transform::createLinearTransform(0.125));
  velocity->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), openvdb::Vec3s(0.25, 0.5, -0.125), true);
  openvdb::io::File file(filename);
  file.write(openvdb::GridPtrVec{density, temperature, emission, velocity});
  file.close();
}
inline openvdb::Vec3SGrid::Ptr motion_velocity_grid(float speed = 96) {
  // Nonzero background deliberately tests advection outside the active box.
  // A small active box with a long trail catches upstream padding truncation.
  auto grid = openvdb::Vec3SGrid::create(openvdb::Vec3s(speed, 0, 0));
  grid->setName("vel");
  grid->setTransform(openvdb::math::Transform::createLinearTransform(0.125));
  grid->setVectorType(openvdb::VEC_INVARIANT);
  grid->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), openvdb::Vec3s(speed, 0, 0), true);
  return grid;
}
inline void write_volume_motion_fixtures(const std::filesystem::path &directory) {
  std::filesystem::create_directories(directory);
  const auto write = [&](const char *name, const openvdb::GridPtrVec &grids) {
    openvdb::io::File file((directory / name).string());
    file.write(grids);
    file.close();
  };
  auto density = density_grid();
  auto velocity = motion_velocity_grid();
  write("vector.vdb", {density, velocity});
  auto integer = openvdb::Int32Grid::create(0);
  integer->setName("integer_field");
  integer->setTransform(density->transform().copy());
  integer->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 1, true);
  write("integer.vdb", {integer});
  auto empty = openvdb::FloatGrid::create(0.f);
  empty->setName("density");
  empty->setTransform(density->transform().copy());
  write("empty.vdb", {empty});
  write("slow.vdb", {density, motion_velocity_grid(48)});
  write("reverse.vdb", {density, motion_velocity_grid(-96)});
  auto temperature = density->deepCopy();
  temperature->setName("temperature");
  temperature->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 2.0f, true);
  auto emission = density->deepCopy();
  emission->setName("emission");
  emission->fill(openvdb::CoordBBox(openvdb::Coord(-4), openvdb::Coord(4)), 0.25f, true);
  write("pyro.vdb", {density, temperature, emission, velocity});
  auto affine_density = density->deepCopy();
  affine_density->transform().postScale(openvdb::Vec3d(0.5, 1, 2));
  affine_density->transform().postRotate(1.5707963267948966, openvdb::math::Z_AXIS);
  auto affine_velocity = velocity->deepCopy();
  affine_velocity->transform().postScale(openvdb::Vec3d(0.25, 2, 1));
  affine_velocity->transform().postTranslate(openvdb::Vec3d(0.25, 0.5, 0));
  write("affine.vdb", {affine_density, affine_velocity});
  openvdb::GridPtrVec scalar_fields{density};
  for (int axis = 0; axis < 3; ++axis) {
    auto scalar = openvdb::FloatGrid::create(axis ? 0.0f : 96.0f);
    scalar->setName(std::string("vel.") + "xyz"[axis]);
    scalar->setTransform(density->transform().copy());
    scalar->fill(openvdb::CoordBBox(openvdb::Coord(0), openvdb::Coord(7)), axis ? 0.0f : 96.0f, true);
    scalar_fields.push_back(scalar);
  }
  write("components.vdb", scalar_fields);
  auto mismatched = scalar_fields.back()->deepCopyGrid();
  mismatched->transform().postTranslate(openvdb::Vec3d(0.25, 0, 0));
  scalar_fields.back() = mismatched;
  write("components-mismatch.vdb", scalar_fields);
  auto invalid = velocity->deepCopy();
  invalid->tree().setValue(openvdb::Coord(0), openvdb::Vec3s(std::numeric_limits<float>::quiet_NaN(), 0, 0));
  write("nonfinite.vdb", {density, invalid});
}
} // namespace cycles_houdini::harness
