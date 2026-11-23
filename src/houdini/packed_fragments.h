// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "geometry_selection.h"
#include <GU/GU_DetailHandle.h>
#include <cstddef>
#include <string>
#include <utility>
#include <vector>
class GU_PrimPacked;

namespace cycles_houdini {
// The contents of a packed primitive as one in-memory detail, the form the
// rest of the adapter consumes. A packed geometry primitive returns its own
// detail. A packed fragment (Assemble; Pack with a name attribute) refers to
// the primitives of one shared detail whose fragment attribute holds its
// value: it returns a detail holding exactly those primitives, as an Unpack
// SOP without the packed transform would. Each shared detail is grouped by
// value once; fragments are built in parallel batches and kept while their
// shared detail lives unchanged, so a frame in which only packed transforms
// move returns the same details (same unique ids: poses only downstream).
// `index` is the primitive's position in `selection`, the packed selection
// of `container`; the following fragments are built in the same batch.
// An empty handle means no in-memory contents; for a fragment,
// packed_fragment_problem() says why. Callable from any thread (one lock;
// the batch inside runs isolated on all cores).
GU_ConstDetailHandle packed_contents(const GU_Detail &container, const NativeGeometrySelection &selection,
                                     std::size_t index, const GU_PrimPacked &primitive);
// Why a packed fragment has no contents, for its diagnostic. Empty for any
// other primitive.
std::string packed_fragment_problem(const GU_PrimPacked &primitive);
// Cumulative process counters (tests compare differences); any thread,
// never waits for a build.
std::vector<std::pair<const char *, double>> packed_fragment_stats();
} // namespace cycles_houdini
