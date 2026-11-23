// SPDX-License-Identifier: Apache-2.0
#include "packed_blender_bounds.h"
#include <UT/UT_JSONValue.h>
#include <UT/UT_String.h>
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <list>
#include <mutex>
#include <stdexcept>
#include <unordered_map>
#ifdef _WIN32
#define NOMINMAX
#include <Windows.h>
#endif

namespace cycles_houdini {
namespace {
constexpr std::size_t max_node_bytes = 1024 * 1024;
constexpr std::size_t max_node_records = 128;
constexpr std::size_t max_record_bytes = 128 * 1024;
constexpr std::size_t max_cache_bytes = 16 * 1024 * 1024;
constexpr std::size_t max_entries = 1024;
constexpr std::size_t max_dependencies = 256;
struct Key {
  std::string reference;
  double frame;
  std::string identity;
  bool operator==(const Key &other) const {
    return reference == other.reference && frame == other.frame && identity == other.identity;
  }
};
struct Hash {
  std::size_t operator()(const Key &key) const {
    return std::hash<std::string>{}(key.reference) ^ (std::hash<double>{}(key.frame) << 1) ^
           (std::hash<std::string>{}(key.identity) << 2);
  }
};
struct Entry {
  std::shared_ptr<BlenderReferenceBoundsLease> lease;
  std::list<Key>::iterator position;
  std::size_t bytes;
};
std::mutex cache_mutex;
std::list<Key> recency;
std::unordered_map<Key, Entry, Hash> cache;
// Weak entries keep live primitive leases coherent after LRU eviction. They
// retain no payload; expired entries are pruned in bounded publication batches.
std::unordered_map<Key, std::weak_ptr<BlenderReferenceBoundsLease>, Hash> leases;
std::unordered_map<Key, std::weak_ptr<BlenderReferencePreviewLease>, Hash> preview_leases;
std::unordered_map<Key, bool, Hash> retired_previews;
std::deque<Key> retired_preview_order;
std::size_t publications = 0;
std::size_t resident_bytes = 0;

void invalidatePreview(const std::shared_ptr<BlenderReferencePreviewLease> &lease) {
  // Only retire authority here. Held GU details may have concurrent readers;
  // never mutate their ordinary HDK box caches from a publication callback.
  // The owning SOP's next cook replaces its cosmetic viewport bounds.
  lease->valid.store(false);
}
void erase(const Key &key, bool invalidate = true) {
  const Key owned_key = key; // callers may pass a key stored in cache/recency
  if (invalidate) {
    const auto lease = leases.find(owned_key);
    if (lease != leases.end()) {
      if (auto live = lease->second.lock())
        live->valid.store(false);
      leases.erase(lease);
    }
  }
  const auto it = cache.find(owned_key);
  if (it != cache.end()) {
    if (invalidate)
      it->second.lease->valid.store(false);
    resident_bytes -= it->second.bytes;
    recency.erase(it->second.position);
    cache.erase(it);
  }
}
void retain(const Key &key, const BlenderReferenceBounds &bounds) {
  std::lock_guard lock(cache_mutex);
  const auto previous = cache.find(key);
  if (previous != cache.end() && previous->second.lease->bounds.serialized == bounds.serialized) {
    recency.splice(recency.begin(), recency, previous->second.position);
    return;
  }
  std::shared_ptr<BlenderReferenceBoundsLease> lease;
  const auto retained = leases.find(key);
  if (retained != leases.end())
    lease = retained->second.lock();
  if (!lease || lease->bounds.serialized != bounds.serialized) {
    erase(key, true);
    lease = std::make_shared<BlenderReferenceBoundsLease>();
    lease->bounds = bounds;
  }
  const auto bytes = 3 * key.reference.size() + bounds.serialized.size() + bounds.source_identity.size() + 256;
  recency.push_front(key);
  leases[key] = lease;
  cache.emplace(key, Entry{std::move(lease), recency.begin(), bytes});
  resident_bytes += bytes;
  while (resident_bytes > max_cache_bytes || cache.size() > max_entries)
    erase(recency.back(), false);
  if (++publications % 64 == 0)
    for (auto it = leases.begin(); it != leases.end();)
      if (it->second.expired())
        it = leases.erase(it);
      else
        ++it;
}
std::string signature(const std::string &path, std::int64_t *mtime = nullptr, std::uint64_t *size = nullptr) {
  if (path.empty() || path.size() > 32768 || path.find('\0') != std::string::npos)
    return {};
  const auto native_path = std::filesystem::u8path(path);
#ifdef _WIN32
  const auto handle =
      CreateFileW(native_path.c_str(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                  nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
  if (handle == INVALID_HANDLE_VALUE)
    return {};
  BY_HANDLE_FILE_INFORMATION info{};
  FILE_BASIC_INFO basic{};
  const bool ok = GetFileInformationByHandle(handle, &info) &&
                  GetFileInformationByHandleEx(handle, FileBasicInfo, &basic, sizeof(basic));
  CloseHandle(handle);
  if (!ok)
    return {};
  const auto amount = (std::uint64_t(info.nFileSizeHigh) << 32) | info.nFileSizeLow;
  const auto modified = (std::uint64_t(info.ftLastWriteTime.dwHighDateTime) << 32) | info.ftLastWriteTime.dwLowDateTime;
  if (mtime)
    *mtime = std::int64_t(modified - 116444736000000000ULL) * 100;
  if (size)
    *size = amount;
  return std::to_string(modified) + ":" + std::to_string(basic.ChangeTime.QuadPart) + ":" + std::to_string(amount) +
         ":" + std::to_string(info.dwVolumeSerialNumber) + ":" + std::to_string(info.nFileIndexHigh) + ":" +
         std::to_string(info.nFileIndexLow);
#else
  std::error_code error;
  const auto modified = std::filesystem::last_write_time(native_path, error);
  if (error)
    return {};
  const auto amount = std::filesystem::file_size(native_path, error);
  if (error)
    return {};
  if (size)
    *size = amount;
  return std::to_string(modified.time_since_epoch().count()) + ":" + std::to_string(amount);
#endif
}
bool parse(const std::string &reference, double frame, const std::string &text, UT_JSONValue &value,
           BlenderReferenceBounds &result) {
  if (!std::isfinite(frame) || reference.empty() || reference.size() > 32768 || text.size() > max_record_bytes ||
      text.find('\0') != std::string::npos || !value.parseValue(UT_StringHolder(text)) || !value.isMap())
    return false;
  const auto schema = value.find("schema_version"), ref = value.find("reference"), sample = value.find("frame"),
             known = value.find("known"), identity = value.find("source_identity"), box = value.find("bounds");
  if (schema.atEnd() || schema->getType() != UT_JSONValue::JSON_INT || schema->getI() != 1 || ref.atEnd() ||
      ref->getType() != UT_JSONValue::JSON_STRING || ref->getString().toStdString() != reference || sample.atEnd() ||
      (sample->getType() != UT_JSONValue::JSON_INT && sample->getType() != UT_JSONValue::JSON_REAL) ||
      sample->getF() != frame || known.atEnd() || known->getType() != UT_JSONValue::JSON_BOOL || !known->getB() ||
      identity.atEnd() || identity->getType() != UT_JSONValue::JSON_STRING || identity->getString().length() != 64 ||
      box.atEnd() || !box->isArray())
    return false;
  int count = 0;
  for (const auto &coordinate : *box) {
    if (count == 6 ||
        (coordinate.getType() != UT_JSONValue::JSON_INT && coordinate.getType() != UT_JSONValue::JSON_REAL))
      return false;
    result.box[count++] = coordinate.getF();
  }
  if (count != 6)
    return false;
  for (int i = 0; i < 6; ++i)
    if (!std::isfinite(result.box[i]))
      return false;
  for (int i = 0; i < 3; ++i)
    if (result.box[i] > result.box[i + 3])
      return false;
  const auto selector = value.find("selector"), kind = value.find("selector_type"), source = value.find("source"),
             dependencies = value.find("dependency_versions"), disabled = value.find("include_excluded_collections");
  if (selector.atEnd() || selector->getType() != UT_JSONValue::JSON_STRING || !selector->getString().isstring() ||
      kind.atEnd() || kind->getType() != UT_JSONValue::JSON_STRING || source.atEnd() ||
      source->getType() != UT_JSONValue::JSON_STRING || dependencies.atEnd() || !dependencies->isMap() ||
      (!disabled.atEnd() && disabled->getType() != UT_JSONValue::JSON_BOOL))
    return false;
  const auto target_kind = kind->getString().toStdString();
  // A collection evaluated with view-layer-excluded children belongs only to
  // the path.blend#collection+disabled=Name spelling of its reference.
  const bool include_disabled = !disabled.atEnd() && disabled->getB();
  std::string suffix;
  if (target_kind == "OBJECT" && !include_disabled)
    suffix = "#object=";
  else if (target_kind == "COLLECTION")
    suffix = include_disabled ? "#collection+disabled=" : "#collection=";
  if (suffix.empty() || reference.find('\0') != std::string::npos ||
      reference.size() <= suffix.size() + selector->getString().length() ||
      reference.compare(reference.size() - suffix.size() - selector->getString().length(), std::string::npos,
                        suffix + selector->getString().toStdString()) != 0)
    return false;
  std::size_t dependency_count = 0;
  for (const auto &[name, version] : dependencies->items()) {
    if (++dependency_count > max_dependencies || name.length() == 0 || name.length() > 32768 || !version.isArray())
      return false;
    std::size_t fields = 0;
    for (const auto &field : version)
      if (++fields > 4 || field.getType() != UT_JSONValue::JSON_INT)
        return false;
    if (fields != 4)
      return false;
  }
  if (!dependency_count || dependencies->find(source->getString()).atEnd())
    return false;
  for (const auto c : identity->getString().toStdString())
    if (!std::isxdigit(static_cast<unsigned char>(c)))
      return false;
  result.source_identity = identity->getString().toStdString();
  return true;
}
bool signatures_match(const UT_JSONValue &value) {
  const auto signatures = value.find("native_signatures"), dependencies = value.find("dependency_versions"),
             source = value.find("source");
  if (signatures.atEnd() || !signatures->isMap() || dependencies.atEnd() || !dependencies->isMap() || source.atEnd() ||
      dependencies->find(source->getString()).atEnd())
    return false;
  std::size_t count = 0;
  for (const auto &[name, version] : signatures->items()) {
    if (++count > max_dependencies || version.getType() != UT_JSONValue::JSON_STRING ||
        dependencies->find(name).atEnd() || signature(name.toStdString()) != version.getString().toStdString())
      return false;
  }
  std::size_t dependency_count = 0;
  for (const auto &[name, expected] : dependencies->items()) {
    if (++dependency_count > max_dependencies || signatures->find(name).atEnd() || !expected.isArray())
      return false;
    std::size_t fields = 0;
    for (const auto &field : expected) {
      if (++fields > 4 || (field.getType() != UT_JSONValue::JSON_INT && field.getType() != UT_JSONValue::JSON_REAL) ||
          !std::isfinite(field.getF()))
        return false;
    }
    if (fields != 4)
      return false;
  }
  return count != 0 && count == dependency_count;
}
bool source_matches_reference(const std::string &reference, const UT_JSONValue &value) {
  const auto source = value.find("source");
  if (source.atEnd() || source->getType() != UT_JSONValue::JSON_STRING)
    return false;
  std::string lower = reference;
  std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return char(std::tolower(c)); });
  const auto object = lower.find(".blend#object="), collection = lower.find(".blend#collection="),
             disabled = lower.find(".blend#collection+disabled=");
  const auto end = std::min(std::min(object, collection), disabled);
  if (end == std::string::npos)
    return false;
  // The const-char constructor borrows; a temporary substring must be owned.
  UT_String raw(reference.substr(0, end + 6));
  raw.expandVariables();
  auto path = std::filesystem::u8path(raw.toStdString());
  if (!path.is_absolute()) {
    UT_String hip("$HIP");
    hip.expandVariables();
    path = std::filesystem::u8path(hip.toStdString()) / path;
  }
  std::error_code error;
  return std::filesystem::equivalent(path, std::filesystem::u8path(source->getString().toStdString()), error) && !error;
}
} // namespace

std::shared_ptr<const BlenderReferencePreviewLease>
lease_blender_reference_preview(const std::string &reference, double frame, const std::string &serialized) {
  BlenderReferenceBounds record;
  if (!preview_blender_reference_bounds(reference, frame, serialized, record))
    return {};
  std::lock_guard lock(cache_mutex);
  const Key key{reference, frame, record.source_identity};
  if (retired_previews.find(key) != retired_previews.end())
    return {};
  const auto found = preview_leases.find(key);
  if (found != preview_leases.end())
    if (auto live = found->second.lock(); live && live->valid.load())
      return live;
  for (auto it = preview_leases.begin(); it != preview_leases.end();)
    if (it->second.expired())
      it = preview_leases.erase(it);
    else
      ++it;
  if (preview_leases.size() >= max_entries && preview_leases.find(key) == preview_leases.end())
    return {};
  auto token = std::make_shared<BlenderReferencePreviewLease>();
  token->source_identity = record.source_identity;
  preview_leases[key] = token;
  return token;
}
bool blender_reference_preview_valid(const std::string &reference, double frame, const std::string &identity) {
  if (reference.empty() || reference.size() > 32768 || !std::isfinite(frame) || identity.size() != 64 ||
      identity.find_first_not_of("0123456789abcdef") != std::string::npos)
    return false;
  std::lock_guard lock(cache_mutex);
  return retired_previews.find(Key{reference, frame, identity}) == retired_previews.end();
}
void invalidate_blender_reference_preview(const std::string &reference, double frame, const std::string &identity) {
  if (reference.empty() || reference.size() > 32768 || !std::isfinite(frame) || identity.size() != 64 ||
      identity.find_first_not_of("0123456789abcdef") != std::string::npos)
    return;
  std::lock_guard lock(cache_mutex);
  const Key key{reference, frame, identity};
  const auto found = preview_leases.find(key);
  if (found != preview_leases.end())
    if (auto live = found->second.lock())
      invalidatePreview(live);
  if (retired_previews.emplace(key, true).second)
    retired_preview_order.push_back(key);
  while (retired_preview_order.size() > max_entries) {
    retired_previews.erase(retired_preview_order.front());
    retired_preview_order.pop_front();
  }
}
void restore_blender_reference_preview(const std::string &reference, double frame, const std::string &identity) {
  if (reference.empty() || reference.size() > 32768 || !std::isfinite(frame) || identity.size() != 64 ||
      identity.find_first_not_of("0123456789abcdef") != std::string::npos)
    return;
  std::lock_guard lock(cache_mutex);
  const Key key{reference, frame, identity};
  retired_previews.erase(key);
  retired_preview_order.erase(std::remove(retired_preview_order.begin(), retired_preview_order.end(), key),
                              retired_preview_order.end());
  const auto found = preview_leases.find(key);
  if (found != preview_leases.end())
    if (auto live = found->second.lock(); live && !live->valid.load())
      preview_leases.erase(found); // Old held GU details stay retired.
}
bool preview_blender_reference_bounds(const std::string &reference, double frame, const std::string &text,
                                      BlenderReferenceBounds &out) {
  UT_JSONValue value;
  BlenderReferenceBounds result;
  if (!parse(reference, frame, text, value, result))
    return false;
  result.serialized = text;
  out = std::move(result);
  return true;
}
bool select_saved_blender_reference_bounds(const std::string &text, const std::string &reference, double frame,
                                           BlenderReferenceBounds &out) {
  if (text.empty() || text.size() > max_node_bytes)
    return false;
  UT_JSONValue envelope;
  if (!envelope.parseValue(UT_StringHolder(text)) || !envelope.isMap())
    return false;
  const auto schema = envelope.find("schema_version"), records = envelope.find("records");
  if (schema.atEnd() || schema->getType() != UT_JSONValue::JSON_INT || schema->getI() != 1 || records.atEnd() ||
      !records->isArray())
    return false;
  std::size_t count = 0;
  bool found = false;
  for (const auto &record : *records) {
    if (++count > max_node_records)
      return false;
    BlenderReferenceBounds candidate;
    if (preview_blender_reference_bounds(reference, frame, record.toString().toStdString(), candidate)) {
      // Duplicate keys make a tampered envelope ambiguous.
      if (found)
        return false;
      out = std::move(candidate);
      found = true;
    }
  }
  return found;
}
std::string serialized_blender_reference_bounds(const std::string &reference, double frame) {
  BlenderReferenceBounds bounds;
  return lookup_blender_reference_bounds(reference, frame, bounds) ? bounds.serialized : std::string();
}
bool lookup_blender_reference_bounds(const std::string &reference, double frame, BlenderReferenceBounds &out) {
  const auto lease = lease_blender_reference_bounds(reference, frame);
  if (!lease || !lease->valid.load(std::memory_order_relaxed))
    return false;
  out = lease->bounds;
  return true;
}
std::shared_ptr<const BlenderReferenceBoundsLease> lease_blender_reference_bounds(const std::string &reference,
                                                                                  double frame) {
  std::lock_guard lock(cache_mutex);
  const auto it = cache.find(Key{reference, frame});
  if (it == cache.end()) {
    const auto retained = leases.find(Key{reference, frame});
    return retained == leases.end() ? nullptr : retained->second.lock();
  }
  recency.splice(recency.begin(), recency, it->second.position);
  return it->second.lease;
}
void publish_blender_reference_bounds(const std::string &reference, double frame, const std::string &text) {
  UT_JSONValue value;
  BlenderReferenceBounds result;
  if (!parse(reference, frame, text, value, result)) {
    // A deliberate unknown result retires a previous box rather than claiming
    // that the unit placeholder conservatively bounds displaced geometry.
    if (text.size() <= max_record_bytes && value.isMap()) {
      const auto known = value.find("known"), ref = value.find("reference"), sample = value.find("frame");
      if (!known.atEnd() && known->getType() == UT_JSONValue::JSON_BOOL && !known->getB() && !ref.atEnd() &&
          ref->getString().toStdString() == reference && !sample.atEnd() && sample->getF() == frame) {
        std::lock_guard lock(cache_mutex);
        erase(Key{reference, frame});
        return;
      }
    }
    throw std::invalid_argument("Malformed packed Blender bounds record");
  }
  const auto dependencies = value.find("dependency_versions"), source = value.find("source");
  if (dependencies.atEnd() || !dependencies->isMap() || source.atEnd() ||
      dependencies->find(source->getString()).atEnd() || !source_matches_reference(reference, value))
    throw std::invalid_argument("Packed bounds omit or mismatch their resolved source dependency");
  // A saved record already carries the evaluation's exact native revision.
  // Never overwrite stale change-time/file-ID evidence with a newer signature.
  if (!value.find("native_signatures").atEnd() && !signatures_match(value))
    throw std::invalid_argument("Packed bounds dependency changed before restoration publication");
  UT_JSONValue signatures(UT_JSONValue::JSON_MAP);
  std::size_t count = 0;
  for (const auto &[name, expected] : dependencies->items()) {
    if (++count > max_dependencies)
      throw std::invalid_argument("Packed bounds dependency budget exceeded");
    std::int64_t mtime = 0;
    std::uint64_t size = 0;
    const auto observed = signature(name.toStdString(), &mtime, &size);
    if (observed.empty())
      throw std::invalid_argument("Packed bounds dependency is unavailable");
    const auto saved_signatures = value.find("native_signatures");
    if (!saved_signatures.atEnd() && saved_signatures->find(name)->getString().toStdString() != observed)
      throw std::invalid_argument("Packed bounds dependency changed during restoration publication");
#ifdef _WIN32
    // Check evaluation's revision before capturing the native signature, closing
    // the race between the host's final freshness check and this publication.
    int fields = 0;
    if (expected.isArray())
      for (const auto &field : expected)
        ++fields;
    if (fields != 4 || expected[int64(0)].getI() != mtime || std::uint64_t(expected[int64(2)].getI()) != size)
      throw std::invalid_argument("Packed bounds dependency changed before publication");
#endif
    signatures.appendMap(name, UT_JSONValue(UT_StringHolder(observed)));
  }
  value["native_signatures"] = signatures;
  result.serialized = value.toString().toStdString();
  if (result.serialized.size() > max_record_bytes || !signatures_match(value))
    throw std::invalid_argument("Packed bounds changed or exceed their persistence budget");
  retain(Key{reference, frame}, result);
}
bool restore_blender_reference_bounds(const std::string &reference, double frame, const std::string &text,
                                      BlenderReferenceBounds &out) {
  UT_JSONValue value;
  BlenderReferenceBounds restored;
  if (!parse(reference, frame, text, value, restored) || !source_matches_reference(reference, value) ||
      !signatures_match(value))
    return false;
  restored.serialized = text;
  retain(Key{reference, frame}, restored);
  out = std::move(restored);
  return true;
}
bool validate_blender_reference_bounds(const std::string &reference, double frame, BlenderReferenceBounds &out) {
  BlenderReferenceBounds candidate;
  if (!lookup_blender_reference_bounds(reference, frame, candidate))
    return false;
  UT_JSONValue value;
  BlenderReferenceBounds parsed;
  if (parse(reference, frame, candidate.serialized, value, parsed) && source_matches_reference(reference, value) &&
      signatures_match(value)) {
    out = std::move(candidate);
    return true;
  }
  std::lock_guard lock(cache_mutex);
  const auto it = leases.find(Key{reference, frame});
  if (it != leases.end())
    if (const auto lease = it->second.lock(); lease && lease->bounds.serialized == candidate.serialized)
      erase(Key{reference, frame});
  return false;
}
void clear_blender_reference_bounds() {
  std::lock_guard lock(cache_mutex);
  for (auto &[key, entry] : leases)
    if (auto lease = entry.lock())
      lease->valid.store(false);
  for (auto &[key, entry] : preview_leases)
    if (auto lease = entry.lock())
      invalidatePreview(lease);
  preview_leases.clear();
  retired_previews.clear();
  retired_preview_order.clear();
  cache.clear();
  leases.clear();
  recency.clear();
  resident_bytes = 0;
}
} // namespace cycles_houdini
