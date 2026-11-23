// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "volume_fixture.h"
#include "kernel/svm/fractal_noise.h"

namespace cycles_houdini::harness {
// Procedural demonstration fields, not a simulated pyro cache. Coordinates
// and values are explicit; the editable example embeds the resulting VDBs.
inline void write_showcase_volume(const std::string &filename) {
  constexpr float voxel = .055f;
  auto density = openvdb::FloatGrid::create(0.0f);
  density->setName("density");
  density->setGridClass(openvdb::GRID_FOG_VOLUME);
  density->setTransform(openvdb::math::Transform::createLinearTransform(voxel));
  auto temperature = density->deepCopy();
  temperature->setName("temperature");
  auto emission = density->deepCopy();
  emission->setName("emission");
  auto da = density->getAccessor(), ta = temperature->getAccessor(), ea = emission->getAccessor();
  for (int y = 0; y < 66; ++y)
    for (int z = -25; z <= 25; ++z)
      for (int x = -25; x <= 25; ++x) {
        const float px = x * voxel, py = y * voxel, pz = z * voxel;
        const float cx = .20f * std::sin(py * 3.2f), cz = .15f * std::cos(py * 4.1f);
        const float radius = .20f + .19f * py;
        const float r2 = ((px - cx) * (px - cx) + (pz - cz) * (pz - cz)) / (radius * radius);
        const auto p = ccl::make_float3(px * 3.8f, py * 2.5f, pz * 3.8f);
        const float turbulence = ccl::noise_fbm(p, 3.5f, .65f, 2.0f, true);
        const float value =
            std::exp(-r2 * 1.7f) * std::max(0.0f, 1.0f - py / 3.55f) * std::max(0.0f, (turbulence - .22f) * 2.4f);
        if (value < .008f)
          continue;
        const openvdb::Coord coord(x, y, z);
        da.setValue(coord, value);
        ta.setValue(coord, .8f + 1.4f * std::exp(-py * .8f));
        ea.setValue(coord, value * std::exp(-py * 1.1f));
      }
  density->tree().prune();
  temperature->tree().prune();
  emission->tree().prune();
  openvdb::io::File file(filename);
  file.write(openvdb::GridPtrVec{density, temperature, emission});
  file.close();
}
} // namespace cycles_houdini::harness
