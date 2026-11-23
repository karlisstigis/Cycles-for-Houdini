// SPDX-License-Identifier: Apache-2.0
// Diagnostic comparison of the host NanoVDB format and kernel accessors.
#include "util/defines.h"
#include "util/math.h"
#include "util/nanovdb.h"
#include "kernel/util/nanovdb.h"
#include "volume_fixture.h"
#include <iostream>
#include <stdexcept>
template <class KernelT, class HostT> void inspect(int precision) {
  auto source = cycles_houdini::harness::density_grid();
  auto handle = ccl::openvdb_to_nanovdb(source, precision, 0);
  auto *kernel = reinterpret_cast<const ccl::nanovdb::NanoGrid<KernelT> *>(handle.data());
  const auto *host = handle.grid<HostT>();
  if (!host)
    throw std::runtime_error("Unexpected NanoVDB build type");
  std::cout << "{\"precision\":" << precision << ",\"kernel_grid_bytes\":" << sizeof(*kernel)
            << ",\"host_grid_bytes\":" << sizeof(*host) << ",\"kernel_table\":" << kernel->tree().root().mTableSize
            << ",\"host_table\":" << host->tree().root().tileCount() << "}\n";
  ccl::nanovdb::ReadAccessor<KernelT> closest(kernel->tree().root());
  ccl::nanovdb::CachedReadAccessor<KernelT> cached(kernel->tree().root());
  for (int x = -5; x <= 5; ++x) {
    const auto coordinate = ccl::make_int3(x, 0, 0);
    const auto expected = source->tree().getValue(openvdb::Coord(x, 0, 0));
    if (closest.getValue(coordinate) != expected || cached.getValue(coordinate) != expected)
      throw std::runtime_error("Host/kernel NanoVDB lookup mismatch");
    std::cout << "{\"precision\":" << precision << ",\"x\":" << x
              << ",\"host\":" << source->tree().getValue(openvdb::Coord(x, 0, 0))
              << ",\"closest\":" << closest.getValue(coordinate) << ",\"cached\":" << cached.getValue(coordinate)
              << "}\n";
  }
}
int main() {
  try {
    inspect<float, float>(32);
    inspect<ccl::nanovdb::Fp16, ::nanovdb::Fp16>(16);
    inspect<ccl::nanovdb::FpN, ::nanovdb::FpN>(0);
    std::cout << "{\"result\":\"passed\",\"checks\":33}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
