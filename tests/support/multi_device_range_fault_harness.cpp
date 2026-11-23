/* SPDX-License-Identifier: Apache-2.0 */
/* Standalone dependency substitutes around verbatim production method bodies.
 * This executes control flow and owner byte copies without GPU SDKs or a renderer. */
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <iostream>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>
using namespace std;
using device_ptr = uintptr_t;
constexpr unsigned MEM_FLAG_PER_DEVICE = 1;
#define LOG_ERROR cerr

struct DeviceMemoryRange {
  size_t offset, size;
};
// @RANGE_POLICY@
// @RANGE_VALIDATION@
struct Stats {
  size_t mem_used = 0;
  function<void(const char *, size_t, double)> buffer_copy_callback;
};
double time_dt() { return 0; }
// Stay above the production cost-based full-copy threshold for two sparse runs.
constexpr size_t allocation_bytes = 1024 * 1024;
class Device;
struct device_memory {
  Device *device;
  device_ptr device_pointer;
  size_t device_size;
  unsigned flags = 0;
  vector<unsigned char> host;
  void *host_pointer;
  bool need_realloc_ = false;
  device_memory(Device *owner)
      : device(owner), device_pointer(77), device_size(allocation_bytes), host(allocation_bytes, 0x29),
        host_pointer(host.data()) {}
  size_t memory_size() const { return host.size(); }
  const char *global_name() const { return "range-fault"; }
  void device_copy_to();
  bool device_copy_to_ranges(const vector<DeviceMemoryRange> &ranges,
                             DeviceMemoryRangePolicy policy = DeviceMemoryRangePolicy::EXACT);
};
class Device {
public:
  string error_msg;
  Stats stats;
  virtual ~Device() = default;
  virtual const string &error_message() { return error_msg; }
  // @HAVE_ERROR@
  virtual void set_error(const string &error);
  virtual bool mem_copy_to_range_supported(device_memory &) { return true; }
  virtual void mem_copy_to(device_memory &) {
    throw std::runtime_error("Sparse-range fixture unexpectedly used full copy");
  }
  virtual void mem_copy_to_range(device_memory &, size_t, size_t) = 0;
};
// @SET_ERROR@
// @COPY_TO@
// @COPY_TO_RANGES@
class Child : public Device {
public:
  int calls = 0, bad_pointer_calls = 0;
  bool cpu_noop = false, fail_copy = false;
  vector<unsigned char> bytes = vector<unsigned char>(allocation_bytes, 0x81);
  bool mem_copy_to_range_supported(device_memory &mem) override {
    return mem.device == this && mem.device_pointer == reinterpret_cast<device_ptr>(bytes.data());
  }
  void mem_copy_to_range(device_memory &mem, size_t offset, size_t size) override {
    ++calls;
    if (mem.device != this || mem.device_pointer != reinterpret_cast<device_ptr>(bytes.data())) {
      ++bad_pointer_calls;
      return; // CPU's no-op must not be relied on to detect absent allocations.
    }
    if (fail_copy) {
      set_error("injected backend transfer failure");
      return;
    }
    if (!cpu_noop)
      copy_n(mem.host.begin() + offset, size, bytes.begin() + offset);
  }
};
class MultiDevice : public Device {
public:
  struct SubDevice {
    unique_ptr<Child> device = make_unique<Child>();
    map<device_ptr, device_ptr> ptr_map;
  };
  using Island = span<SubDevice *const>;
  list<SubDevice> devices;
  vector<vector<SubDevice *>> peer_islands;
  mutable shared_mutex ptr_map_mutex;
  // @PTR_MAP_FIND@
  // @PTR_MAP_CONTAINS@
  // @FIND_OWNER@
  // @ERROR_MESSAGE@
  // @SUPPORTED@
  // @TRANSFER@
};
class FaultMulti : public MultiDevice {
public:
  function<void()> after_preflight;
  bool mem_copy_to_range_supported(device_memory &mem) override {
    const bool supported = MultiDevice::mem_copy_to_range_supported(mem);
    if (supported && after_preflight)
      after_preflight();
    return supported;
  }
};
static int checks = 0, cases = 0;
void require(bool condition, const char *message) {
  ++checks;
  if (!condition)
    throw runtime_error(message);
}
struct Fixture {
  FaultMulti multi;
  device_memory mem{&multi};
  vector<MultiDevice::SubDevice *> owners;
  Fixture() {
    for (int i = 0; i < 3; ++i) {
      multi.devices.emplace_back();
      auto *sub = &multi.devices.back();
      sub->ptr_map[mem.device_pointer] = reinterpret_cast<device_ptr>(sub->device->bytes.data());
      owners.push_back(sub);
      multi.peer_islands.push_back({sub});
    }
  }
  void restored() {
    require(mem.device == &multi, "borrowed device was not restored");
    require(mem.device_pointer == 77, "virtual key was not restored");
    require(mem.device_size == allocation_bytes, "allocation size was not restored");
  }
  void clean_destinations() {
    for (auto *sub : owners)
      require(sub->device->bad_pointer_calls == 0, "invalid child destination submitted");
  }
  void visible_error(int owner) {
    require(owners[owner]->device->have_error(), "missing selected child error");
    const string message = multi.error_message();
    require(!message.empty(), "missing aggregate error");
    for (int i = 0; i < 3; ++i) {
      require(multi.have_error(), "aggregate error disappeared");
      require(multi.error_message() == message, "aggregate error changed on query");
    }
  }
};
void missing_map(int missing, bool cpu_noop, bool direct, bool zero_map) {
  Fixture f;
  f.owners[missing]->device->cpu_noop = cpu_noop;
  require(f.multi.MultiDevice::mem_copy_to_range_supported(f.mem), "preflight must initially pass");
  f.restored();
  auto inject = [&] {
    if (zero_map)
      f.owners[missing]->ptr_map[77] = 0;
    else
      f.owners[missing]->ptr_map.erase(77);
  };
  if (direct) {
    inject();
    f.multi.mem_copy_to_range(f.mem, 3, 5);
  } else {
    f.multi.after_preflight = inject;
    // true means selected: it must not request a full-copy fallback after partial transfer.
    require(f.mem.device_copy_to_ranges({{3, 5}, {17, 4}}), "failed partial copy requested full-copy retry");
  }
  f.restored();
  f.clean_destinations();
  f.visible_error(missing);
  for (int i = 0; i < 3; ++i) {
    require(f.owners[i]->device->calls == (i < missing ? 1 : 0),
            "transfer continued after missing mapping or submitted later range");
    for (size_t byte = 0; byte < allocation_bytes; ++byte) {
      const bool updated = i < missing && byte >= 3 && byte < 8;
      require(f.owners[i]->device->bytes[byte] == (updated ? 0x29 : 0x81), "partial owner bytes or canary incorrect");
    }
  }
  ++cases;
}
void normal_copy() {
  Fixture f;
  // Two peers share one owner; range copy must visit that owner just once.
  f.multi.peer_islands = {{f.owners[0], f.owners[1]}, {f.owners[2]}};
  f.owners[1]->ptr_map.clear();
  require(f.mem.device_copy_to_ranges({{3, 5}, {17, 4}}), "normal copy declined");
  require(!f.multi.have_error(), "normal copy set error");
  f.restored();
  f.clean_destinations();
  for (int i = 0; i < 3; ++i) {
    require(f.owners[i]->device->calls == (i == 1 ? 0 : 2), "peer owner copied wrong number of times");
    for (size_t byte = 0; byte < allocation_bytes; ++byte) {
      bool updated = i != 1 && ((byte >= 3 && byte < 8) || (byte >= 17 && byte < 21));
      require(f.owners[i]->device->bytes[byte] == (updated ? 0x29 : 0x81), "normal copy bytes or canary incorrect");
    }
  }
  ++cases;
}
void unsupported() {
  Fixture f;
  f.multi.peer_islands = {{f.owners[0], f.owners[1]}, {f.owners[2]}};
  f.mem.flags = MEM_FLAG_PER_DEVICE;
  require(!f.mem.device_copy_to_ranges({{3, 5}}), "per-device peers must use full-copy fallback");
  require(!f.multi.have_error(), "preflight fallback must not set error");
  f.restored();
  for (auto *sub : f.owners)
    require(sub->device->calls == 0, "preflight fallback started transfer");
  f.mem.flags = 0;
  f.owners[2]->ptr_map.clear();
  require(!f.mem.device_copy_to_ranges({{3, 5}}), "preflight missing owner must decline");
  const vector<DeviceMemoryRange> scattered{{1, 1}, {3, 1}, {5, 1}, {7, 1}, {9, 1}, {11, 1}, {13, 1}, {15, 1}};
  require(!f.mem.device_copy_to_ranges(scattered, DeviceMemoryRangePolicy::COMPLETE_HOST),
          "Whole-mirror policy bypassed ownership preflight");
  require(!f.multi.have_error(), "missing preflight owner should permit full-copy fallback");
  f.restored();
  for (auto *sub : f.owners)
    require(sub->device->calls == 0, "Missing owner started transfer");
  ++cases;
}
void backend_failure() {
  Fixture f;
  f.owners[1]->device->fail_copy = true;
  require(f.mem.device_copy_to_ranges({{3, 5}, {17, 4}}), "backend failure requested full-copy retry");
  f.restored();
  f.clean_destinations();
  f.visible_error(1);
  require(f.owners[0]->device->calls == 1 && f.owners[1]->device->calls == 1 && f.owners[2]->device->calls == 0,
          "backend failure did not stop islands and ranges");
  ++cases;
}

class RecordingDevice : public Device {
public:
  vector<unsigned char> bytes;
  vector<DeviceMemoryRange> copies;
  vector<size_t> observed_bytes;
  explicit RecordingDevice(size_t size) : bytes(size, 0x81) {
    stats.buffer_copy_callback = [&](const char *, size_t count, double) { observed_bytes.push_back(count); };
  }
  void mem_copy_to(device_memory &mem) override {
    copies.push_back({0, mem.memory_size()});
    copy(mem.host.begin(), mem.host.end(), bytes.begin());
  }
  void mem_copy_to_range(device_memory &mem, size_t offset, size_t size) override {
    copies.push_back({offset, size});
    copy_n(mem.host.begin() + offset, size, bytes.begin() + offset);
  }
};
template <typename T> class device_vector : public device_memory {
public:
  device_vector(RecordingDevice &owner, size_t bytes) : device_memory(&owner) {
    host.resize(bytes);
    host_pointer = host.data();
    device_size = bytes;
  }
  size_t size() const { return host.size(); }
  bool copy_to_device_ranges(const vector<DeviceMemoryRange> &ranges) { return device_copy_to_ranges(ranges); }
  void copy_to_device() { device_copy_to(); }
};
struct DeviceMemoryDirtyRanges {
  vector<DeviceMemoryRange> values;
  const vector<DeviceMemoryRange> &ranges() const { return values; }
};
// @LIGHT_TREE_UPLOAD_RESULT@
template <typename T>
// @LIGHT_TREE_UPLOAD@

void copy_policy() {
  constexpr size_t small = 65539, poison = 22;
  const DeviceMemoryDirtyRanges dirty{{{3, 17}, {33, 9}, {53, 31}}};
  for (size_t size : {small, allocation_bytes}) {
    for (const auto policy : {DeviceMemoryRangePolicy::EXACT, DeviceMemoryRangePolicy::COMPLETE_HOST}) {
      RecordingDevice device(size);
      device_vector<unsigned char> mem(device, size);
      mem.host[poison] = 0x5c;
      bool rejected = false;
      try {
        mem.device_copy_to_ranges({{0, 1}, {size, 1}}, policy);
      } catch (const runtime_error &) {
        rejected = true;
      }
      require(rejected && device.copies.empty() && device.observed_bytes.empty(),
              "Invalid policy range touched the device");
      require(mem.device_copy_to_ranges(dirty.ranges(), policy), "Valid policy rejected");
      const bool full = size == small && policy == DeviceMemoryRangePolicy::COMPLETE_HOST;
      const vector<DeviceMemoryRange> expected = full ? vector<DeviceMemoryRange>{{0, size}} : dirty.ranges();
      vector<unsigned char> readback(size, 0x81);
      vector<size_t> sizes;
      require(device.copies.size() == expected.size(), "Unexpected transfer count for policy");
      for (size_t i = 0; i < expected.size(); ++i) {
        const auto &copy = expected[i];
        require(device.copies[i].offset == copy.offset && device.copies[i].size == copy.size,
                "Unexpected transfer offset or size for policy");
        copy_n(mem.host.begin() + copy.offset, copy.size, readback.begin() + copy.offset);
        sizes.push_back(copy.size);
      }
      require(readback == device.bytes, "Policy lost range bytes or modified outside bytes");
      require(device.observed_bytes == sizes, "Policy telemetry differs from submitted transfers");
      ++cases;
    }
    RecordingDevice device(size);
    device_vector<unsigned char> mem(device, size);
    const auto reported = light_tree_upload(mem, dirty, true);
    size_t copied = 0;
    for (const auto &copy : device.copies)
      copied += copy.size;
    require(reported.partial && reported.spans == 3 && reported.bytes == 57, "Light-tree range policy changed");
    require(reported.spans == device.copies.size() && reported.bytes == copied,
            "Light-tree statistics disagree with dispatched transfers");
    require(device.bytes[poison] == 0x81, "Light-tree copy touched an unselected byte");
    ++cases;

    RecordingDevice dense_device(size);
    device_vector<unsigned char> dense(dense_device, size);
    const DeviceMemoryDirtyRanges all{{{0, size}}};
    const auto full = light_tree_upload(dense, all, true);
    require(!full.partial && full.spans == 1 && full.bytes == size, "Light-tree whole policy changed");
    require(dense_device.copies.size() == 1 && dense_device.copies[0].offset == 0 &&
                dense_device.copies[0].size == size && dense_device.observed_bytes == vector<size_t>{size},
            "Light-tree whole statistics disagree with dispatched transfer");
    require(dense_device.bytes == dense.host, "Light-tree whole transfer did not retain the full mirror");
    ++cases;
  }
}

int main() {
  try {
    for (int missing : {0, 1, 2})
      for (bool cpu : {false, true})
        for (bool direct : {false, true})
          for (bool zero_map : {false, true})
            missing_map(missing, cpu, direct, zero_map);
    normal_copy();
    unsupported();
    backend_failure();
    copy_policy();
    cout << "{\"result\":\"passed\",\"cases\":" << cases << ",\"checks\":" << checks << "}\n";
    return 0;
  } catch (const exception &error) {
    cerr << error.what() << '\n';
    return 1;
  }
}
