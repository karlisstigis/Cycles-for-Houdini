// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/mesh_pointiness.h"
#include <chrono>
#include <iostream>

namespace cycles_houdini::harness {
inline void mesh_pointiness_contracts() {
  const auto started = std::chrono::steady_clock::now();
  int checks = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  const auto evaluate = [](const std::vector<Float3> &points, const std::vector<std::vector<int>> &faces) {
    return mesh_pointiness(points, [&](auto emit) {
      for (const auto &face : faces)
        emit(std::span<const int>(face));
    });
  };
  const auto uniform = [](const auto &values, float expected) {
    return std::all_of(values.begin(), values.end(),
                       [&](float value) { return std::isfinite(value) && std::abs(value - expected) < 2e-5f; });
  };
  const std::vector<Float3> octa{{1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, 1}, {0, 0, -1}};
  const std::vector<std::vector<int>> faces{{0, 2, 4}, {0, 5, 2}, {0, 4, 3}, {0, 3, 5},
                                            {1, 4, 2}, {1, 2, 5}, {1, 3, 4}, {1, 5, 3}};
  require(uniform(evaluate(octa, faces), .75f), "Convex Pointiness disagrees with the analytic octahedron");
  auto reversed = faces;
  for (auto &face : reversed)
    std::reverse(face.begin(), face.end());
  require(uniform(evaluate(octa, reversed), .25f), "Concave Pointiness disagrees with reversed normals");
  auto scaled = octa;
  for (auto &p : scaled) {
    p.x *= 3;
    p.y *= 3;
    p.z *= 3;
  }
  require(uniform(evaluate(scaled, faces), .75f), "Uniform scale changed intrinsic curvature");
  std::vector<Float3> soup;
  std::vector<std::vector<int>> polygons;
  for (const auto &face : faces) {
    std::vector<int> polygon;
    for (int index : face) {
      polygon.push_back(int(soup.size()));
      soup.push_back(octa[index]);
    }
    polygons.push_back(std::move(polygon));
  }
  require(uniform(evaluate(soup, polygons), .75f), "Split polygon vertices failed coordinate welding");
  for (std::size_t i = 0; i < soup.size(); ++i) {
    if (soup[i].x == 0)
      soup[i].x = float(int(i % 3) - 1) * 1e-8f;
  }
  require(uniform(evaluate(soup, polygons), .75f), "Nearby split vertices failed the documented welding radius");
  std::vector<Float3> grid;
  std::vector<std::vector<int>> quads;
  for (int copy = 0; copy < 3; ++copy) {
    const int offset = int(grid.size());
    for (int y = 0; y < 5; ++y)
      for (int x = 0; x < 5; ++x)
        grid.push_back({float(x - 2), float(y - 2), 0});
    for (int y = 0; y < 4; ++y)
      for (int x = 0; x < 4; ++x) {
        const int a = offset + y * 5 + x;
        quads.push_back({a, a + 1, a + 6, a + 5});
      }
  }
  require(uniform(evaluate(grid, quads), .5f), "Equal coordinate sums and duplicate chains changed a flat field");
  auto isolated = octa;
  isolated.push_back({8, 8, 8});
  auto field = evaluate(isolated, faces);
  require(field.size() == isolated.size() && field.back() == 0, "An isolated mesh vertex has invalid Pointiness");
  require(uniform(std::vector<float>(field.begin(), field.end() - 1), .75f),
          "An isolated vertex changed connected curvature");
  const std::vector<Float3> extreme{
      {-2e38f, -2e38f, 2e38f}, {2e38f, -2e38f, 2e38f}, {2e38f, 2e38f, 2e38f}, {-2e38f, 2e38f, 2e38f}};
  require(uniform(evaluate(extreme, {{0, 1, 2, 3}}), .5f),
          "Finite extreme coordinates broke sorting or geometric normals");
  std::vector<Float3> coincident(20000, Float3{});
  field = evaluate(coincident, {});
  require(field.size() == coincident.size() && uniform(field, 0),
          "High-multiplicity coincident vertices are not finite/isolated");
  field = evaluate({{0, 0, 0}, {1, 0, 0}}, {{0, 0, 1}});
  require(std::all_of(field.begin(), field.end(), [](float v) { return std::isfinite(v) && v >= 0 && v <= 1; }),
          "Degenerate source polygons produced invalid intrinsic values");
  require(evaluate({}, {}).empty(), "An empty mesh produced Pointiness values");
  std::cout << "{\"result\":\"passed\",\"pointiness_checks\":" << checks
            << ",\"coincident_vertices\":20000,\"seconds\":"
            << std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count() << "}\n";
}
} // namespace cycles_houdini::harness
