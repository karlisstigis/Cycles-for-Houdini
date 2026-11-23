// SPDX-License-Identifier: Apache-2.0
#include "ies_assets.h"
#include "texture_assets.h"
#include "util/ies.h"
#include "util/md5.h"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <locale>
#include <map>
#include <mutex>
#include <sstream>
#include <vector>
namespace cycles_houdini {
namespace {
constexpr std::size_t text_limit = 16 * 1024 * 1024, sample_limit = 1024 * 1024;
struct CachedText {
  std::weak_ptr<const std::string> text;
  std::string content_revision;
};
std::mutex text_cache_mutex;
std::map<std::string, CachedText> text_cache; // Weak payloads; at most 256 small lookup records.
void validate_text(const std::string &text) {
  if (text.empty() || text.size() > text_limit || text.find('\0') != std::string::npos)
    throw IesSourceError("IES needs nonempty text of at most 16 MiB without embedded NULs");
  const auto tilt = text.find("\nTILT=");
  if (tilt == std::string::npos)
    throw IesSourceError("IES is missing its TILT header");
  const auto end = text.find('\n', tilt + 1);
  if (end == std::string::npos)
    throw IesSourceError("IES has no photometric data");
  auto mode = text.substr(tilt + 6, end - tilt - 6);
  if (!mode.empty() && mode.back() == '\r')
    mode.pop_back();
  // The pinned engine does not evaluate external tilt files or tilt factors.
  // Do not silently claim to have interpreted them.
  if (mode != "NONE")
    throw IesSourceError("IES tilt data requires an additional adapter; use TILT=NONE");
  auto body = text.substr(end + 1);
  std::replace(body.begin(), body.end(), ',', ' ');
  std::istringstream input(body);
  input.imbue(std::locale::classic());
  std::vector<double> values;
  std::string token;
  while (input >> token) {
    const auto at = values.size();
    const auto first = token.data() + (token.front() == '+' ? 1 : 0);
    double value{};
    const auto parsed = std::from_chars(first, token.data() + token.size(), value);
    if (parsed.ec != std::errc{} || parsed.ptr != token.data() + token.size())
      throw IesSourceError("IES contains an unreadable numeric token");
    // Cycles uses strtol for these header fields. Reject decimal/exponent
    // spellings which would shift its parser to different count tokens.
    if (at == 0 || at == 3 || at == 4 || at == 5 || at == 6) {
      const auto digits = (token.front() == '+' || token.front() == '-') ? 1u : 0u;
      if (token.size() == digits || token.find_first_not_of("0123456789", digits) != std::string::npos ||
          std::abs(value) > std::numeric_limits<int>::max())
        throw IesSourceError("IES integer header fields require bounded integer tokens");
    }
    if (!std::isfinite(value) || values.size() > sample_limit + 16384)
      throw IesSourceError("IES contains nonfinite or excessive photometric data");
    values.push_back(value);
  }
  if (!input.eof() || values.size() < 13)
    throw IesSourceError("IES photometric data is unreadable or truncated");
  const auto count = [&](std::size_t at, int minimum) {
    const auto number = values[at];
    if (number < minimum || number > 4096 || std::floor(number) != number)
      throw IesSourceError("IES angle counts are invalid or exceed 4096 per axis");
    return static_cast<std::size_t>(number);
  };
  const auto vertical = count(3, 2), horizontal = count(4, 1);
  if (vertical * horizontal > sample_limit || values.size() < 13 + vertical + horizontal + vertical * horizontal)
    throw IesSourceError("IES intensity table is truncated or exceeds 1,048,576 samples");
  if (values[5] != 1 && values[5] != 2 && values[5] != 3)
    throw IesSourceError("IES has an unsupported photometric type");
  for (const auto [start, size] : {std::pair{std::size_t(13), vertical}, std::pair{13 + vertical, horizontal}})
    for (std::size_t n = 0; n < size; ++n)
      if (std::abs(values[start + n]) > 360 || (n && values[start + n] <= values[start + n - 1]))
        throw IesSourceError("IES angles must increase strictly and lie within -360..360 degrees");
  const double factor = values[2] * values[10] * values[11] * .0706650768394;
  if (!std::isfinite(factor) || factor < 0)
    throw IesSourceError("IES photometric multipliers must be finite and nonnegative");
  for (std::size_t at = 13 + vertical + horizontal; at < 13 + vertical + horizontal + vertical * horizontal; ++at)
    if (values[at] < 0 || factor * values[at] > std::numeric_limits<float>::max())
      throw IesSourceError("IES intensities must fit nonnegative finite float32 values");
  ccl::IESFile parsed;
  if (!parsed.load(text) || parsed.packed_size() <= 0 || parsed.packed_size() > 16 * 1024 * 1024)
    throw IesSourceError("Cycles cannot interpret this IES distribution");
  std::vector<float> packed(static_cast<std::size_t>(parsed.packed_size()));
  parsed.pack(packed.data());
  int processed_horizontal{}, processed_vertical{};
  std::memcpy(&processed_horizontal, packed.data(), sizeof(int));
  std::memcpy(&processed_vertical, packed.data() + 1, sizeof(int));
  if (processed_horizontal < 2 || processed_vertical < 2)
    throw IesSourceError("IES processing needs at least two samples along each axis");
  if (std::any_of(packed.begin(), packed.end(), [](float item) { return !std::isfinite(item); }))
    throw IesSourceError("IES processing produced unusable photometric values");
}
} // namespace
std::shared_ptr<const std::string> prepare_ies_asset(const std::string &filename, const std::string &text,
                                                     IesAsset &asset) {
  asset = {};
  if (text.empty()) {
    asset.filename = filename;
    asset.revision = filename.empty() ? std::string{} : texture_watch_revision({filename});
  }
  std::string cache_key;
  if (text.empty())
    cache_key = "file:" + texture_watch_path(filename) + '\0' + asset.revision;
  else {
    ccl::MD5Hash hash;
    hash.append(text);
    asset.content_revision = hash.get_hex();
    cache_key = "inline:" + asset.content_revision;
    asset.filename.clear();
  }
  {
    std::lock_guard lock(text_cache_mutex);
    std::erase_if(text_cache, [](const auto &entry) { return entry.second.text.expired(); });
    if (const auto cached = text_cache.find(cache_key); cached != text_cache.end())
      if (auto shared = cached->second.text.lock()) {
        asset.content_revision = cached->second.content_revision;
        return shared;
      }
  }
  std::string content = text;
  if (content.empty()) {
    if (filename.empty() || filename.find('\0') != std::string::npos || filename.starts_with("op:"))
      throw IesSourceError("Choose a readable IES file or supply owned IES text");
    try {
      const auto path = std::filesystem::u8path(filename);
      if (!std::filesystem::is_regular_file(path))
        throw IesSourceError("Missing IES file: " + filename);
      const auto bytes = std::filesystem::file_size(path);
      if (!bytes || bytes > text_limit)
        throw IesSourceError("IES file must contain 1 byte..16 MiB: " + filename);
      std::ifstream stream(path, std::ios::binary);
      content.resize(static_cast<std::size_t>(bytes));
      if (!stream.read(content.data(), static_cast<std::streamsize>(bytes)))
        throw IesSourceError("Unreadable IES file: " + filename);
    } catch (const std::filesystem::filesystem_error &error) {
      throw IesSourceError("Cannot read IES file: " + filename + ": " + error.what());
    }
  }
  validate_text(content);
  if (asset.content_revision.empty()) {
    ccl::MD5Hash hash;
    hash.append(content);
    asset.content_revision = hash.get_hex();
  }
  auto result = std::make_shared<const std::string>(std::move(content));
  {
    std::lock_guard lock(text_cache_mutex);
    while (text_cache.size() >= 256)
      text_cache.erase(text_cache.begin());
    text_cache[cache_key] = {result, asset.content_revision};
  }
  return result;
}
bool ies_asset_changed(const IesAsset &asset) {
  return !asset.filename.empty() && texture_watch_revision({asset.filename}) != asset.revision;
}
} // namespace cycles_houdini
