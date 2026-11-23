// SPDX-License-Identifier: Apache-2.0
// Packed fragments as per-fragment details: see packed_fragments.h and
// docs/dev/notes/native-packed-fragments.md.
#include "packed_fragments.h"
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <map>
#include <mutex>
#include <numeric>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <GA/GA_Handle.h>
#include <GA/GA_Iterator.h>
#include <GA/GA_OffsetList.h>
#include <GA/GA_Range.h>
#include <GU/GU_Detail.h>
#include <GU/GU_PackedFragment.h>
#include <GU/GU_PrimPacked.h>
#include <UT/UT_ParallelUtil.h>

namespace cycles_houdini {
namespace {
using Clock = std::chrono::steady_clock;
double seconds_since(Clock::time_point started) {
  return std::chrono::duration<double>(Clock::now() - started).count();
}
// One batch builds at most this many fragments (or source primitives), so a
// host turn stays bounded while every core has work.
constexpr std::size_t batch_fragments = 8192;
constexpr std::size_t batch_primitives = std::size_t(1) << 21;
constexpr auto npos = std::size_t(-1);

bool env_flag(const char *name) {
  const auto *value = std::getenv(name);
  return value && std::strcmp(value, "1") == 0;
}
// The adapter's switch for its slot conversion: equivalence tests compare the
// parallel schedule with a serial one.
bool serial() { return env_flag("CYCLES_HOUDINI_SERIAL_CONVERSION"); }
// Debug check: compare every built fragment with the HDK's own fragment range.
bool check_ranges() { return env_flag("CYCLES_HOUDINI_CHECK_FRAGMENTS"); }

// Runs while the cache lock is held. Isolation keeps this thread from
// stealing an outer task (such as another conversion slot) that could call
// back into the cache and wait on that lock.
template <class Body> void parallel_range(std::size_t count, std::size_t grain, const Body &body) {
  if (count <= grain || serial()) {
    body(std::size_t{0}, count);
    return;
  }
  UTisolate([&] {
    UTparallelFor(UT_BlockedRange<std::size_t>(0, count, grain),
                  [&](const UT_BlockedRange<std::size_t> &range) { body(range.begin(), range.end()); });
  });
}

// Data ids change whenever an attribute or the topology is written, so a
// shared detail edited in place under the same unique id is noticed.
std::vector<std::int64_t> detail_stamp(const GU_Detail &detail) {
  std::vector<std::int64_t> stamp = {std::int64_t(detail.getMetaCacheCount()),
                                     std::int64_t(detail.getTopology().getDataId()),
                                     std::int64_t(detail.getPrimitiveList().getDataId()),
                                     std::int64_t(detail.getNumPoints()), std::int64_t(detail.getNumPrimitives())};
  for (const auto owner : {GA_ATTRIB_VERTEX, GA_ATTRIB_POINT, GA_ATTRIB_PRIMITIVE, GA_ATTRIB_DETAIL})
    for (auto it = detail.getAttributeDict(owner).begin(GA_SCOPE_PUBLIC); !it.atEnd(); ++it)
      stamp.push_back(std::int64_t((*it)->getDataId()));
  return stamp;
}

// The fragment's primitives in ascending offset order, as the HDK defines
// them (linear in the shared detail: only for the fallback and the check).
std::vector<GA_Offset> hdk_offsets(const GU_PackedFragment &fragment) {
  std::vector<GA_Offset> result;
  for (GA_Iterator it(fragment.getPrimitiveRange()); !it.atEnd(); ++it)
    result.push_back(*it);
  std::sort(result.begin(), result.end());
  return result;
}

// A new detail takes the next process-wide unique id, which the adapter uses
// as a prototype identity: allocate in fragment order on one thread, so ids
// (and the snapshot) do not depend on the schedule; fill in parallel.
GU_DetailHandle allocate() {
  GU_DetailHandle handle;
  handle.allocateAndSet(new GU_Detail, true);
  return handle;
}
void fill(GU_Detail &detail, const GU_Detail &shared, const GA_Offset *begin, const GA_Offset *end) {
  GA_OffsetList list;
  list.reserve(GA_Size(end - begin));
  for (auto *it = begin; it != end; ++it)
    list.append(*it);
  detail.mergePrimitives(shared, GA_Range(shared.getPrimitiveMap(), list));
}
GU_ConstDetailHandle materialize(const GU_Detail &shared, const GA_Offset *begin, const GA_Offset *end) {
  auto handle = allocate();
  fill(*handle.gdpNC(), shared, begin, end);
  return handle;
}

// One shared detail grouped by one fragment attribute.
struct Partition {
  GU_ConstDetailHandle source; // Evicted once nothing but this cache holds it.
  std::vector<std::int64_t> stamp;
  std::string attribute;
  // A primitive string or integer attribute is grouped once; anything else
  // is resolved fragment by fragment through the HDK's range (`fallback`).
  bool grouped{}, integer{}, missing{};
  std::unordered_map<std::string, std::size_t> strings;
  std::unordered_map<exint, std::size_t> integers;
  std::vector<GA_Offset> offsets;  // Grouped by value, ascending within a group.
  std::vector<std::size_t> starts; // Group g holds offsets[starts[g], starts[g + 1]).
  std::vector<GU_ConstDetailHandle> contents;
  std::vector<unsigned char> built, checked;
  std::map<std::string, GU_ConstDetailHandle> fallback;
};

struct Stats {
  double partitions{}, partition_seconds{}, partition_primitives{}, built{}, build_seconds{}, batches{}, reused{},
      empty{}, fallback{}, evicted{}, checked{};
};

// Callable from any thread: one lock guards the partitions; the counters
// are published to a separately locked copy, so reading them never waits
// for a build and touches no geometry.
class Cache {
public:
  GU_ConstDetailHandle contents(const GU_Detail &container, const NativeGeometrySelection &selection, std::size_t index,
                                const GU_PackedFragment &fragment);
  std::string problem(const GU_PackedFragment &fragment);
  std::vector<std::pair<const char *, double>> stats() const;

private:
  Partition *partition(const GU_PackedFragment &fragment);
  void group(Partition &partition);
  static std::size_t find(const Partition &partition, const GU_PackedFragment &fragment);
  void check(Partition &partition, std::size_t group, const GU_PackedFragment &fragment);
  void sweep();

  void publish();

  std::mutex mutex_;
  std::map<std::pair<std::int64_t, std::string>, Partition> partitions_;
  Stats stats_;
  mutable std::mutex published_mutex_;
  Stats published_;
  std::size_t published_partitions_{};
};

void Cache::publish() {
  std::lock_guard lock(published_mutex_);
  published_ = stats_;
  published_partitions_ = partitions_.size();
}

Partition *Cache::partition(const GU_PackedFragment &fragment) {
  const auto &handle = fragment.detailPtr();
  const auto *shared = handle.gdp();
  if (!shared)
    return nullptr;
  auto name = fragment.attribute().toStdString();
  auto stamp = detail_stamp(*shared);
  auto &result = partitions_[{shared->getUniqueId(), name}];
  if (result.source.gdp() == shared && result.stamp == stamp)
    return &result;
  result = Partition{};
  result.source = handle;
  result.stamp = std::move(stamp);
  result.attribute = std::move(name);
  group(result);
  return &result;
}

// Group the shared detail's primitives by attribute value once: read every
// key in parallel, sort (key, primitive index) pairs in parallel, cut runs.
// Ties are impossible, so the result is independent of the schedule.
void Cache::group(Partition &partition) {
  const auto started = Clock::now();
  const auto &shared = *partition.source.gdp();
  const auto *attribute = shared.findPrimitiveAttribute(partition.attribute.c_str());
  partition.missing = !attribute;
  GA_ROHandleS strings(attribute && attribute->getStorageClass() == GA_STORECLASS_STRING ? attribute : nullptr);
  GA_ROHandleID integers(attribute && attribute->getStorageClass() == GA_STORECLASS_INT ? attribute : nullptr);
  if (!strings.isValid() && !integers.isValid())
    return;
  partition.grouped = true;
  partition.integer = integers.isValid();
  const auto count = std::size_t(shared.getNumPrimitives());
  std::vector<std::pair<std::int64_t, std::size_t>> keys(count);
  parallel_range(count, 16384, [&](std::size_t begin, std::size_t end) {
    for (auto i = begin; i < end; ++i) {
      const auto offset = shared.primitiveOffset(GA_Index(i));
      keys[i] = {partition.integer ? std::int64_t(integers.get(offset)) : std::int64_t(strings.getIndex(offset)), i};
    }
  });
  if (count < 65536 || serial())
    std::sort(keys.begin(), keys.end());
  else
    UTisolate([&] { UTparallelSort(keys.begin(), keys.end()); });
  partition.offsets.resize(count);
  parallel_range(count, 16384, [&](std::size_t begin, std::size_t end) {
    for (auto i = begin; i < end; ++i)
      partition.offsets[i] = shared.primitiveOffset(GA_Index(keys[i].second));
  });
  for (std::size_t i = 0; i < count; ++i) {
    if (i && keys[i].first == keys[i - 1].first)
      continue;
    const auto group = partition.starts.size();
    partition.starts.push_back(i);
    if (partition.integer)
      partition.integers.emplace(exint(keys[i].first), group);
    else
      partition.strings.emplace(strings.get(partition.offsets[i]).toStdString(), group);
  }
  partition.starts.push_back(count);
  partition.contents.resize(partition.starts.size() - 1);
  partition.built.assign(partition.contents.size(), 0);
  partition.checked.assign(partition.contents.size(), 0);
  ++stats_.partitions;
  stats_.partition_primitives += double(count);
  stats_.partition_seconds += seconds_since(started);
}

std::size_t Cache::find(const Partition &partition, const GU_PackedFragment &fragment) {
  if (!partition.grouped)
    return npos;
  if (partition.integer) {
    if (fragment.isIntValue()) {
      const auto found = partition.integers.find(fragment.intValue());
      return found == partition.integers.end() ? npos : found->second;
    }
    return npos;
  }
  const auto found = partition.strings.find(fragment.name().toStdString());
  return found == partition.strings.end() ? npos : found->second;
}

void Cache::check(Partition &partition, std::size_t group, const GU_PackedFragment &fragment) {
  if (!check_ranges() || (group != npos && partition.checked[group]))
    return;
  const auto expected = hdk_offsets(fragment);
  std::vector<GA_Offset> actual;
  if (group != npos)
    actual.assign(partition.offsets.begin() + std::ptrdiff_t(partition.starts[group]),
                  partition.offsets.begin() + std::ptrdiff_t(partition.starts[group + 1]));
  if (actual != expected)
    throw std::logic_error("Packed fragment partition differs from the HDK range for " + partition.attribute + " = " +
                           fragment.name().toStdString());
  if (group != npos)
    partition.checked[group] = 1;
  ++stats_.checked;
}

void Cache::sweep() {
  for (auto it = partitions_.begin(); it != partitions_.end();)
    if (it->second.source.getRefCount() <= 1) {
      it = partitions_.erase(it);
      ++stats_.evicted;
    } else
      ++it;
}

GU_ConstDetailHandle Cache::contents(const GU_Detail &container, const NativeGeometrySelection &selection,
                                     std::size_t index, const GU_PackedFragment &fragment) {
  std::lock_guard lock(mutex_);
  struct Publish {
    Cache &cache;
    ~Publish() { cache.publish(); }
  } publish{*this};
  auto *partition = this->partition(fragment);
  if (!partition)
    return {};
  if (!partition->grouped) {
    // Unusual fragment attributes (point-owned, other storage): the HDK's
    // own range, once per fragment value.
    auto [found, inserted] = partition->fallback.try_emplace(fragment.name().toStdString());
    if (inserted) {
      const auto offsets = hdk_offsets(fragment);
      ++stats_.fallback;
      if (!offsets.empty())
        found->second = materialize(*partition->source.gdp(), offsets.data(), offsets.data() + offsets.size());
    } else
      ++stats_.reused;
    if (!found->second)
      ++stats_.empty;
    return found->second;
  }
  const auto group = find(*partition, fragment);
  check(*partition, group, fragment);
  if (group == npos) {
    ++stats_.empty;
    return {};
  }
  if (partition->built[group]) {
    ++stats_.reused;
    return partition->contents[group];
  }
  // A miss: collect this and the following unbuilt fragments of the
  // selection, then build them all at once on every core.
  const auto started = Clock::now();
  sweep();
  partition = this->partition(fragment);
  struct Job {
    Partition *partition;
    std::size_t group;
  };
  std::vector<Job> jobs;
  std::set<std::pair<Partition *, std::size_t>> queued;
  std::size_t primitives = 0;
  const auto count = std::size_t(selection.full_detail ? container.getNumPrimitives() : selection.primitives.entries());
  for (auto i = index; i < count && jobs.size() < batch_fragments && primitives < batch_primitives; ++i) {
    const auto offset = selection.full_detail ? container.primitiveOffset(GA_Index(i)) : selection.primitives(exint(i));
    const auto *packed = dynamic_cast<const GU_PrimPacked *>(container.getPrimitive(offset));
    const auto *next = packed ? dynamic_cast<const GU_PackedFragment *>(packed->sharedImplementation()) : nullptr;
    if (!next)
      continue;
    auto *owner = i == index ? partition : this->partition(*next);
    if (!owner || !owner->grouped)
      continue;
    const auto found = find(*owner, *next);
    check(*owner, found, *next);
    if (found == npos || owner->built[found] || !queued.emplace(owner, found).second)
      continue;
    jobs.push_back({owner, found});
    primitives += owner->starts[found + 1] - owner->starts[found];
  }
  std::vector<GU_DetailHandle> details(jobs.size());
  for (auto &detail : details)
    detail = allocate();
  parallel_range(jobs.size(), 4, [&](std::size_t begin, std::size_t end) {
    for (auto j = begin; j < end; ++j) {
      auto &[owner, found] = jobs[j];
      const auto *first = owner->offsets.data() + owner->starts[found];
      const auto *last = owner->offsets.data() + owner->starts[found + 1];
      fill(*details[j].gdpNC(), *owner->source.gdp(), first, last);
    }
  });
  for (std::size_t j = 0; j < jobs.size(); ++j)
    jobs[j].partition->contents[jobs[j].group] = details[j];
  for (auto &[owner, found] : jobs)
    owner->built[found] = 1;
  ++stats_.batches;
  stats_.built += double(jobs.size());
  stats_.build_seconds += seconds_since(started);
  return partition->contents[group];
}

std::string Cache::problem(const GU_PackedFragment &fragment) {
  const auto value = fragment.attribute().toStdString() + " = \"" + fragment.name().toStdString() + "\"";
  std::lock_guard lock(mutex_);
  auto *partition = this->partition(fragment);
  if (!partition)
    return "Packed fragment (" + value + ") has no loaded shared geometry";
  if (partition->missing)
    return "Packed fragment (" + value + ") refers to a shared geometry without the primitive attribute " +
           partition->attribute;
  return "Packed fragment (" + value + ") matches no primitive of its shared geometry";
}

std::vector<std::pair<const char *, double>> Cache::stats() const {
  std::lock_guard lock(published_mutex_);
  const auto &value = published_;
  return {{"partitions", value.partitions},
          {"partition_primitives", value.partition_primitives},
          {"partition_seconds", value.partition_seconds},
          {"fragments_built", value.built},
          {"build_batches", value.batches},
          {"build_seconds", value.build_seconds},
          {"fragments_reused", value.reused},
          {"fragments_empty", value.empty},
          {"fallback_ranges", value.fallback},
          {"partitions_evicted", value.evicted},
          {"ranges_checked", value.checked},
          {"cached_partitions", double(published_partitions_)}};
}

// Never destroyed: handles must not be released after Houdini has shut down.
Cache &cache() {
  static auto *instance = new Cache;
  return *instance;
}
const GU_PackedFragment *fragment_of(const GU_PrimPacked &primitive) {
  return dynamic_cast<const GU_PackedFragment *>(primitive.sharedImplementation());
}
} // namespace

GU_ConstDetailHandle packed_contents(const GU_Detail &container, const NativeGeometrySelection &selection,
                                     std::size_t index, const GU_PrimPacked &primitive) {
  const auto *fragment = fragment_of(primitive);
  if (!fragment)
    return primitive.getPackedDetail();
  return cache().contents(container, selection, index, *fragment);
}

std::string packed_fragment_problem(const GU_PrimPacked &primitive) {
  const auto *fragment = fragment_of(primitive);
  if (!fragment)
    return {};
  return cache().problem(*fragment);
}

std::vector<std::pair<const char *, double>> packed_fragment_stats() { return cache().stats(); }
} // namespace cycles_houdini
