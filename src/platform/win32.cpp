// SPDX-License-Identifier: Apache-2.0
#include "platform.h"
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <Windows.h>
#include <psapi.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <system_error>
#include <thread>
namespace cycles_houdini::platform {
namespace {
void quoted_json(std::ostream &out, const std::string &value) {
  static constexpr char hex[] = "0123456789abcdef";
  out << '"';
  for (const unsigned char ch : value) {
    if (ch == '"' || ch == '\\')
      out << '\\' << char(ch);
    else if (ch < 32)
      out << "\\u00" << hex[ch >> 4] << hex[ch & 15];
    else
      out << char(ch);
  }
  out << '"';
}
std::string utf8(const WCHAR *text) {
  const int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1, nullptr, 0, nullptr, nullptr);
  if (count <= 0)
    return "Unavailable adapter name";
  std::string result(count, '\0');
  if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1, result.data(), count, nullptr, nullptr))
    return "Unavailable adapter name";
  result.resize(count - 1);
  return result;
}
void segment(std::ostream &out, IDXGIAdapter3 *adapter, DXGI_MEMORY_SEGMENT_GROUP group) {
  DXGI_QUERY_VIDEO_MEMORY_INFO info{};
  const HRESULT result = adapter->QueryVideoMemoryInfo(0, group, &info);
  out << '{';
  if (FAILED(result))
    out << "\"unavailable_hresult\":" << result;
  else
    out << "\"current_usage_bytes\":" << info.CurrentUsage << ",\"budget_bytes\":" << info.Budget
        << ",\"available_for_reservation_bytes\":" << info.AvailableForReservation
        << ",\"current_reservation_bytes\":" << info.CurrentReservation;
  out << '}';
}
} // namespace
std::string process_memory_json() {
  using Microsoft::WRL::ComPtr;
  std::ostringstream out;
  out << "{\"schema_version\":1,\"pid\":" << GetCurrentProcessId()
      << ",\"scope\":\"Whole calling process. Windows working set/private commit; DXGI OS-reported process GPU segment "
         "usage. "
         "Includes host and driver overhead, not per-session allocation or exact physical residency. Node zero of each "
         "hardware adapter.\",\"host\":{";
  PROCESS_MEMORY_COUNTERS_EX host{};
  host.cb = sizeof(host);
  if (K32GetProcessMemoryInfo(GetCurrentProcess(), reinterpret_cast<PROCESS_MEMORY_COUNTERS *>(&host), sizeof(host)))
    out << "\"working_set_bytes\":" << host.WorkingSetSize << ",\"peak_working_set_bytes\":" << host.PeakWorkingSetSize
        << ",\"private_commit_bytes\":" << host.PrivateUsage;
  else
    out << "\"unavailable_win32_error\":" << GetLastError();
  out << "},\"gpu\":{";
  ComPtr<IDXGIFactory1> factory;
  const HRESULT created = CreateDXGIFactory1(IID_PPV_ARGS(&factory));
  if (FAILED(created))
    out << "\"unavailable_hresult\":" << created;
  else {
    out << "\"adapters\":[";
    bool comma = false;
    HRESULT enumeration = S_OK;
    for (UINT index = 0;; ++index) {
      ComPtr<IDXGIAdapter1> candidate;
      enumeration = factory->EnumAdapters1(index, &candidate);
      if (FAILED(enumeration))
        break;
      DXGI_ADAPTER_DESC1 description{};
      const HRESULT described = candidate->GetDesc1(&description);
      if (SUCCEEDED(described) && (description.Flags & DXGI_ADAPTER_FLAG_SOFTWARE))
        continue;
      if (comma)
        out << ',';
      comma = true;
      out << '{';
      if (FAILED(described))
        out << "\"unavailable_hresult\":" << described;
      else {
        std::ostringstream luid;
        luid << std::hex << std::setfill('0') << std::setw(8) << static_cast<DWORD>(description.AdapterLuid.HighPart)
             << ':' << std::setw(8) << description.AdapterLuid.LowPart;
        out << "\"name\":";
        quoted_json(out, utf8(description.Description));
        out << ",\"luid\":";
        quoted_json(out, luid.str());
        out << ",\"node\":0,\"vendor_id\":" << description.VendorId << ",\"device_id\":" << description.DeviceId
            << ",\"dedicated_video_memory_bytes\":" << description.DedicatedVideoMemory;
        ComPtr<IDXGIAdapter3> adapter;
        const HRESULT upgraded = candidate.As(&adapter);
        if (FAILED(upgraded))
          out << ",\"unavailable_hresult\":" << upgraded;
        else {
          out << ",\"local\":";
          segment(out, adapter.Get(), DXGI_MEMORY_SEGMENT_GROUP_LOCAL);
          out << ",\"nonlocal\":";
          segment(out, adapter.Get(), DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL);
        }
      }
      out << '}';
    }
    out << ']';
    if (enumeration != DXGI_ERROR_NOT_FOUND)
      out << ",\"enumeration_error_hresult\":" << enumeration;
  }
  out << "}}";
  return out.str();
}
bool reserve_file(const std::filesystem::path &candidate) {
  const auto file =
      CreateFileW(candidate.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file != INVALID_HANDLE_VALUE) {
    CloseHandle(file);
    return true;
  }
  const auto error = GetLastError();
  if (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS)
    return false;
  throw std::system_error(error, std::system_category(), "Cannot reserve output file");
}
bool publish_file(const std::filesystem::path &temporary, const std::filesystem::path &destination,
                  bool replace_existing) {
  if (!MoveFileExW(temporary.c_str(), destination.c_str(),
                   (replace_existing ? MOVEFILE_REPLACE_EXISTING : 0) | MOVEFILE_WRITE_THROUGH)) {
    const auto error = GetLastError();
    if (!replace_existing && (error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS))
      return false;
    throw std::system_error(error, std::system_category(), "Cannot publish output file");
  }
  return true;
}
std::uint32_t process_id() noexcept { return GetCurrentProcessId(); }
std::uint64_t uptime_milliseconds() noexcept { return GetTickCount64(); }
bool environment_flag(const char *name) {
  char value[8]{};
  return GetEnvironmentVariableA(name, value, sizeof(value)) == 1 && value[0] == '1';
}
void watch_parent_process(std::uint32_t pid) {
  HANDLE parent = OpenProcess(SYNCHRONIZE, FALSE, DWORD(pid));
  if (!parent)
    throw std::runtime_error("Could not observe renderer owner");
  try {
    std::thread([parent]() {
      WaitForSingleObject(parent, INFINITE);
      std::_Exit(0);
    }).detach();
  } catch (...) {
    CloseHandle(parent);
    throw;
  }
}
} // namespace cycles_houdini::platform
