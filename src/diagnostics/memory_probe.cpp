// SPDX-License-Identifier: Apache-2.0
// Independent Windows/CUDA calibration. No renderer or Houdini process is queried.
#include <windows.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cuda.h>
#include <bit>
#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using Microsoft::WRL::ComPtr;

template <class T> T symbol(HMODULE library, const char *name) {
  const auto result = GetProcAddress(library, name);
  if (!result)
    throw std::runtime_error(std::string("Missing CUDA driver symbol: ") + name);
  return reinterpret_cast<T>(result);
}
void checked(CUresult result, const char *operation) {
  if (result != CUDA_SUCCESS)
    throw std::runtime_error(std::string(operation) + " status " + std::to_string(result));
}
void checked(HRESULT result, const char *operation) {
  if (FAILED(result))
    throw std::runtime_error(std::string(operation) + " HRESULT " + std::to_string(result));
}
struct Library {
  HMODULE module{LoadLibraryW(L"nvcuda.dll")};
  Library() {
    if (!module)
      throw std::runtime_error("nvcuda.dll unavailable");
  }
  ~Library() { FreeLibrary(module); }
};
struct Context {
  CUcontext context{};
  decltype(&cuCtxDestroy) destroy;
  ~Context() {
    if (context)
      destroy(context);
  }
};
struct Allocation {
  CUdeviceptr pointer{};
  decltype(&cuMemFree) release;
  ~Allocation() {
    if (pointer)
      release(pointer);
  }
};

// JSON contains only fixed labels, numeric values and ASCII hexadecimal LUIDs.
void snapshot(IDXGIAdapter3 *adapter, unsigned node, const char *phase, int observation = 0) {
  std::cout << "{\"phase\":\"" << phase << "\",\"observation\":" << observation;
  for (const auto segment : {DXGI_MEMORY_SEGMENT_GROUP_LOCAL, DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL}) {
    DXGI_QUERY_VIDEO_MEMORY_INFO info{};
    const auto result = adapter->QueryVideoMemoryInfo(node, segment, &info);
    std::cout << ",\"" << (segment == DXGI_MEMORY_SEGMENT_GROUP_LOCAL ? "local" : "nonlocal") << "\":{";
    if (FAILED(result))
      std::cout << "\"unavailable_hresult\":" << result;
    else
      std::cout << "\"budget_bytes\":" << info.Budget << ",\"current_usage_bytes\":" << info.CurrentUsage
                << ",\"available_for_reservation_bytes\":" << info.AvailableForReservation
                << ",\"current_reservation_bytes\":" << info.CurrentReservation;
    std::cout << "}";
  }
  std::cout << "}\n" << std::flush;
}

int main(int argc, char **argv) {
  try {
    if (argc != 3)
      throw std::runtime_error("Usage: ch_memory_probe CUDA_DEVICE_ORDINAL ALLOCATION_BYTES");
    std::size_t parsed{};
    const int ordinal = std::stoi(argv[1], &parsed);
    if (parsed != std::strlen(argv[1]) || ordinal < 0)
      throw std::runtime_error("Invalid device ordinal");
    const auto bytes = std::stoull(argv[2], &parsed);
    if (parsed != std::strlen(argv[2]) || bytes == 0 || bytes > 2ull * 1024 * 1024 * 1024)
      throw std::runtime_error("Calibration allocation must be 1 byte to 2 GiB");
    Library library;
    const auto init = symbol<decltype(&cuInit)>(library.module, "cuInit");
    const auto get_device = symbol<decltype(&cuDeviceGet)>(library.module, "cuDeviceGet");
    const auto get_name = symbol<decltype(&cuDeviceGetName)>(library.module, "cuDeviceGetName");
    const auto get_pci = symbol<decltype(&cuDeviceGetPCIBusId)>(library.module, "cuDeviceGetPCIBusId");
    const auto get_luid = symbol<decltype(&cuDeviceGetLuid)>(library.module, "cuDeviceGetLuid");
    const auto create = symbol<decltype(&cuCtxCreate)>(library.module, "cuCtxCreate_v2");
    const auto destroy = symbol<decltype(&cuCtxDestroy)>(library.module, "cuCtxDestroy_v2");
    const auto allocate = symbol<decltype(&cuMemAlloc)>(library.module, "cuMemAlloc_v2");
    const auto release = symbol<decltype(&cuMemFree)>(library.module, "cuMemFree_v2");
    const auto fill = symbol<decltype(&cuMemsetD8)>(library.module, "cuMemsetD8_v2");
    const auto sync = symbol<decltype(&cuCtxSynchronize)>(library.module, "cuCtxSynchronize");
    checked(init(0), "cuInit");
    CUdevice device{};
    checked(get_device(&device, ordinal), "cuDeviceGet");
    char name[256]{}, pci[32]{};
    checked(get_name(name, sizeof(name), device), "cuDeviceGetName");
    checked(get_pci(pci, sizeof(pci), device), "cuDeviceGetPCIBusId");
    std::cerr << "CUDA calibration device: " << name << " (" << pci << ")\n";
    LUID luid{};
    unsigned node_mask{};
    static_assert(sizeof(LUID) == 8);
    checked(get_luid(reinterpret_cast<char *>(&luid), &node_mask, device), "cuDeviceGetLuid");
    if (!std::has_single_bit(node_mask))
      throw std::runtime_error("Calibration requires one CUDA device node");
    const auto node = static_cast<unsigned>(std::countr_zero(node_mask));
    ComPtr<IDXGIFactory1> factory;
    checked(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "CreateDXGIFactory1");
    ComPtr<IDXGIAdapter3> adapter;
    for (UINT index = 0;; ++index) {
      ComPtr<IDXGIAdapter1> candidate;
      const auto result = factory->EnumAdapters1(index, &candidate);
      if (result == DXGI_ERROR_NOT_FOUND)
        break;
      checked(result, "EnumAdapters1");
      DXGI_ADAPTER_DESC1 description{};
      checked(candidate->GetDesc1(&description), "GetDesc1");
      if (description.AdapterLuid.LowPart == luid.LowPart && description.AdapterLuid.HighPart == luid.HighPart) {
        checked(candidate.As(&adapter), "IDXGIAdapter3");
        break;
      }
    }
    if (!adapter)
      throw std::runtime_error("No DXGI adapter matches the CUDA LUID");
    std::cout << "{\"pid\":" << GetCurrentProcessId() << ",\"cuda_ordinal\":" << ordinal
              << ",\"allocation_bytes\":" << bytes << ",\"node\":" << node << ",\"luid_low\":" << luid.LowPart
              << ",\"luid_high\":" << luid.HighPart << "}\n";
    snapshot(adapter.Get(), node, "before_context");
    Context context{{}, destroy};
    checked(create(&context.context, 0, device), "cuCtxCreate");
    snapshot(adapter.Get(), node, "context_only");
    Allocation allocation{{}, release};
    checked(allocate(&allocation.pointer, static_cast<std::size_t>(bytes)), "cuMemAlloc");
    checked(fill(allocation.pointer, 0x5a, static_cast<std::size_t>(bytes)), "cuMemsetD8");
    checked(sync(), "cuCtxSynchronize");
    for (int i = 0; i < 10; ++i) {
      snapshot(adapter.Get(), node, "allocated_and_touched", i);
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    checked(release(allocation.pointer), "cuMemFree");
    allocation.pointer = 0;
    snapshot(adapter.Get(), node, "after_free");
    checked(destroy(context.context), "cuCtxDestroy");
    context.context = nullptr;
    for (int i = 0; i < 10; ++i) {
      snapshot(adapter.Get(), node, "after_context_destroy", i);
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
