// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "../core/render_devices.h"
#include "device/memory_range.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <sstream>
#include <thread>
#include <vector>

namespace cycles_houdini::harness {
inline void device_range_contracts(const std::string &selection) {
  std::size_t checks = 0, owners = 0, calls = 0, requested = 0, mapped = 0, aliases = 0;
  const auto require = [&](bool ok, const char *message) {
    ++checks;
    if (!ok)
      throw std::runtime_error(message);
  };
  const auto rejects = [&](auto action) {
    bool rejected = false;
    try {
      action();
    } catch (const std::runtime_error &) {
      rejected = true;
    }
    require(rejected, "Malformed device range was accepted");
  };
  using Range = ccl::DeviceMemoryRange;
  using Ranges = ccl::vector<Range>;
  require(ccl::device_memory_validate_ranges(0, {}) == 0, "Empty byte ranges failed");
  require(ccl::device_memory_validate_ranges(32, {{0, 3}, {7, 9}, {31, 1}}) == 13, "Byte range accounting changed");
  const std::vector<Ranges> invalid_ranges{
      Ranges{{0, 0}},         Ranges{{32, 1}},        Ranges{{31, 2}},
      Ranges{{0, 9}, {8, 1}}, Ranges{{9, 1}, {0, 1}}, Ranges{{1, std::numeric_limits<std::size_t>::max()}}};
  for (const Ranges &bad : invalid_ranges)
    rejects([&] { ccl::device_memory_validate_ranges(32, bad); });
  ccl::DeviceMemoryDirtyRanges dirty;
  rejects([&] { dirty.reset(1, 0, true); });
  rejects([&] { dirty.reset(std::numeric_limits<std::size_t>::max(), 2, true); });
  dirty.reset(0, 80, true);
  require(dirty.ranges().empty(), "Empty dirty allocation is not empty");
  dirty.reset(8193, 80, true);
  require(dirty.ranges().empty(), "Clean rows request an upload");
  for (std::size_t row : std::array<std::size_t, 4>{0, 255, 256, 8192})
    dirty.mark(row);
  auto ranges = dirty.ranges();
  require(ranges.size() == 2 && ranges[0].offset == 0 && ranges[0].size == 512 * 80 && ranges[1].offset == 8192 * 80 &&
              ranges[1].size == 80,
          "Boundary dirty rows are not coalesced exactly");
  rejects([&] { dirty.mark(8193); });
  dirty.reset(100000, 16, true);
  std::array<std::thread, 4> threads;
  for (std::size_t index = 0; index < threads.size(); ++index)
    threads[index] = std::thread([&, index] {
      for (std::size_t row = index; row < 512; row += 4)
        dirty.mark(row);
      dirty.mark(99999);
    });
  for (auto &thread : threads)
    thread.join();
  ranges = dirty.ranges();
  require(ranges.size() == 2 && ranges[0].size == 512 * 16 && ranges[1].offset <= 99999 * 16 &&
              ranges[1].offset + ranges[1].size == 100000 * 16,
          "Concurrent dirty marks lost rows");
  dirty.reset(100000, 16, true);
  for (std::size_t block = 0; block < 130; block += 2)
    dirty.mark(block * 256);
  ranges = dirty.ranges();
  require(ranges.size() == 1 && ranges[0].size == 100000 * 16, "Fragmented dirty marks exceed the transfer bound");
  dirty.reset(1024, 16, true);
  dirty.mark(0);
  dirty.mark(512);
  ranges = dirty.ranges();
  require(ranges.size() == 1 && ranges[0].size == 1024 * 16, "Dense dirty rows did not request a full upload");
  dirty.reset(1025, 16, false);
  ranges = dirty.ranges();
  require(ranges.size() == 1 && ranges[0].size == 1025 * 16, "Invalid prior rows did not request full initialization");

  SessionConfig config;
  config.threads = 2;
  std::stringstream selections(selection);
  std::string id;
  while (std::getline(selections, id, '|'))
    config.device_ids.push_back(id);
  std::vector<std::string> ids;
  const auto info = resolve_render_devices(config, ids);
  ccl::Stats stats;
  ccl::Profiler profiler;
  auto device = ccl::Device::create(info, stats, profiler, true);
  require(device && !device->have_error(), "Device creation failed");
  std::vector<ccl::Device *> devices;
  std::vector<std::string> actual_ids;
  device->foreach_device([&](ccl::Device *sub) {
    devices.push_back(sub);
    actual_ids.push_back(sub->info.id);
  });
  std::sort(ids.begin(), ids.end());
  std::sort(actual_ids.begin(), actual_ids.end());
  require(actual_ids == ids, "Instantiated device membership differs from the explicit selection");
  std::ostringstream modes;
  const auto initial_allocated = stats.mem_used;
  const bool cpu = info.type == ccl::DEVICE_CPU;
  stats.buffer_copy_callback = [&](const char *, std::size_t bytes, double) {
    ++calls;
    requested += bytes;
  };
  struct RangeBuffer : ccl::device_vector<ccl::uchar> {
    using ccl::device_vector<ccl::uchar>::device_vector;
    void adopt_mapped_host() {
      host_only_free();
      host_pointer = shared_pointer;
      move_to_host = false;
    }
  };
  for (int mode = 0; mode < (cpu ? 2 : 6); ++mode) {
    const auto kind = mode % 2 ? ccl::MEM_READ_ONLY : ccl::MEM_READ_WRITE;
    const bool mapped_memory = mode >= 2, alias = mode >= 4;
    RangeBuffer buffer(device.get(), "range_contract", kind);
    buffer.move_to_host = mapped_memory;
    const std::size_t bytes = 65539;
    buffer.alloc(bytes);
    std::vector<ccl::uchar> expected(bytes);
    for (std::size_t i = 0; i < bytes; ++i)
      expected[i] = ccl::uchar((i * 37 + 11) & 255);
    std::memcpy(buffer.data(), expected.data(), bytes);
    std::vector<std::size_t> allocated_bytes, readbacks(devices.size());
    for (auto *sub : devices)
      allocated_bytes.push_back(sub->stats.mem_used);
    buffer.copy_to_device();
    buffer.clear_modified();
    for (std::size_t index = 0; index < devices.size(); ++index) {
      const auto charged = devices[index]->stats.mem_used;
      require(charged >= allocated_bytes[index], "Device memory charge fell during isolated allocation");
      allocated_bytes[index] = charged - allocated_bytes[index];
    }
    require(std::any_of(allocated_bytes.begin(), allocated_bytes.end(), [](auto bytes) { return bytes != 0; }),
            "No leaf device allocated the transfer buffer");
    if (mapped_memory) {
      require(buffer.shared_pointer != nullptr, "Mapped allocation was not exercised");
      ++mapped;
    }
    if (alias) {
      buffer.adopt_mapped_host();
      require(buffer.host_pointer == buffer.shared_pointer, "Mapped host alias was not exercised");
      ++aliases;
    }
    const auto pointer = buffer.device_pointer;
    const auto host = buffer.host_pointer;
    const auto allocated = stats.mem_used;
    const auto read = [&] {
      for (std::size_t index = 0; index < devices.size(); ++index) {
        auto *sub = devices[index];
        const bool owns_buffer = allocated_bytes[index] != 0;
        require(buffer.is_resident(sub) == owns_buffer, "Resident owner disagrees with leaf allocation accounting");
        const auto address = device->mem_device_ptr(buffer, sub);
        require(address != 0, "Subdevice lost access to its owned or shared allocation");
        if (!owns_buffer)
          continue;
        std::vector<ccl::uchar> actual(bytes);
        if (sub->info.type == ccl::DEVICE_CPU)
          std::memcpy(actual.data(), reinterpret_cast<const void *>(address), bytes);
        else {
          buffer.swap_device(sub, buffer.device_size, address);
          auto original_host = buffer.host_pointer;
          buffer.host_pointer = actual.data();
          try {
            buffer.copy_from_device();
          } catch (...) {
            buffer.host_pointer = original_host;
            buffer.restore_device();
            throw;
          }
          buffer.host_pointer = original_host;
          buffer.restore_device();
        }
        require(actual == expected, "Subrange upload changed bytes outside the submitted ranges or lost an owner");
        require(buffer.device == device.get() && buffer.device_pointer == pointer,
                "Readback changed allocation ownership");
        ++readbacks[index];
        ++owners;
      }
    };
    read();
    for (int pass = 0; pass < 4; ++pass) {
      const Ranges edits{{3, 17}, {bytes / 2 + 1, 9}, {bytes - 31, 31}};
      for (const auto &range : edits)
        for (std::size_t i = range.offset; i < range.offset + range.size; ++i)
          expected[i] = ccl::uchar((i * 19 + pass * 31) & 255);
      std::memcpy(buffer.data(), expected.data(), bytes);
      /* GPU copies must not accidentally upload host rows outside the ranges. */
      if (!cpu && !alias)
        buffer.data()[22] ^= 255;
      const auto old_calls = calls, old_requested = requested;
      require(buffer.copy_to_device_ranges(edits), "Selected device declined owned linear ranges");
      require(calls - old_calls == 3 && requested - old_requested == 57,
              "Range copy telemetry reported the full owner");
      require(buffer.host_pointer == host && buffer.device_pointer == pointer && stats.mem_used == allocated,
              "Subrange copy reallocated or changed ownership");
      read();
      std::memcpy(buffer.data(), expected.data(), bytes);
    }
    // A separate, explicit complete-host upload retains the scattered-copy cost policy.
    const Ranges mirrored{{3, 17}, {bytes / 2 + 1, 9}, {bytes - 31, 31}};
    for (const auto &range : mirrored)
      for (std::size_t i = range.offset; i < range.offset + range.size; ++i)
        expected[i] ^= 255;
    std::memcpy(buffer.data(), expected.data(), bytes);
    const auto mirror_calls = calls, mirror_requested = requested;
    require(buffer.copy_to_device_ranges_or_full(mirrored), "Complete host mirror upload was declined");
    require(calls - mirror_calls == 1 && requested - mirror_requested == bytes,
            "Complete host mirror did not use the cheaper full upload");
    require(buffer.host_pointer == host && buffer.device_pointer == pointer && stats.mem_used == allocated,
            "Complete host mirror upload reallocated or changed ownership");
    read();
    const auto old_calls = calls;
    const std::vector<Ranges> invalid_owned{Ranges{{bytes, 1}}, Ranges{{bytes - 1, 2}}, Ranges{{0, 1}, {bytes, 1}},
                                            Ranges{{2, 2}, {1, 1}}};
    for (const Ranges &bad : invalid_owned)
      rejects([&] { buffer.copy_to_device_ranges(bad); });
    require(calls == old_calls, "Rejected range submitted a partial transfer");
    read();
    if (mode)
      modes << ',';
    modes << "{\"mode\":" << mode << ",\"devices\":[";
    for (std::size_t index = 0; index < devices.size(); ++index) {
      const bool owns_buffer = allocated_bytes[index] != 0;
      require(readbacks[index] == (owns_buffer ? 7 : 0), "An owning leaf missed an independent byte readback");
      if (index)
        modes << ',';
      modes << "{\"id\":" << std::quoted(devices[index]->info.id) << ",\"allocated_bytes\":" << allocated_bytes[index]
            << ",\"readbacks\":" << readbacks[index] << ",\"shared_access\":" << (owns_buffer ? "false" : "true")
            << '}';
    }
    modes << "]}";
    require(buffer.copy_to_device_ranges({}), "Empty ranges changed the device");
    buffer.tag_realloc();
    require(!buffer.copy_to_device_ranges({{1, 1}}), "Pending replacement accepted partial initialization");
    buffer.clear_modified();
    buffer.move_to_host = mapped_memory;
    buffer.alloc(bytes + 17);
    require(!buffer.copy_to_device_ranges({{1, 1}}), "Unallocated replacement accepted partial initialization");
    for (std::size_t i = 0; i < buffer.size(); ++i)
      buffer.data()[i] = ccl::uchar(i & 255);
    buffer.copy_to_device();
    buffer.clear_modified();
    require(buffer.copy_to_device_ranges({{bytes + 16, 1}}), "Resized owner failed after full initialization");
    buffer.free();
    require(stats.mem_used == initial_allocated, "Closed range allocation retained device ownership");
  }
  require(!device->have_error(), "Device range operation failed");
  std::cout << "{\"result\":\"passed\",\"checks\":" << checks << ",\"owner_readbacks\":" << owners
            << ",\"copy_calls\":" << calls << ",\"requested_bytes\":" << requested
            << ",\"mapped_allocations\":" << mapped << ",\"shared_host_aliases\":" << aliases
            << ",\"instantiated_devices\":[";
  for (std::size_t index = 0; index < actual_ids.size(); ++index) {
    if (index)
      std::cout << ',';
    std::cout << std::quoted(actual_ids[index]);
  }
  std::cout << "],\"allocation_modes\":[" << modes.str() << "]}" << std::endl;
}
} // namespace cycles_houdini::harness
