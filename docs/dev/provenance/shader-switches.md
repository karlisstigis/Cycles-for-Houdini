# Switch, Enable Output and Index Switch with non-constant selectors (behaviour specification)

Status: behaviour description for a separately written replacement. Written 21 November 2026.
Scope: three stubs in `src/core/shader_switches.cpp`, inside `lower_shader_switches`:
`boolean_constant`, `boolean_endpoint` and `lower_varying`. Everything else in the file stays: the node
catalogue (`shader_switch_definitions`), parameter checks, the 256-node / 2048-link budgets, constant
selection (a selector known at lowering time picks its branch directly), the Warning node, AOV handling,
and `shader_constant_value` (`src/core/shader_primitives.cpp`, which uses Cycles' Apache-2.0
`ConstantFolder`).

Rules that exist only because Blender behaves that way, or because an existing test pins them, are marked
**[interop]** or **[test]** and listed again in section 6.

## 1. Setting

The native Houdini nodes Switch, Enable Output and Index Switch (one per data type: float, integer,
boolean, vector, color, shader) do not exist in Cycles. Before the graph reaches Cycles,
`lower_shader_switches` replaces each of them by ordinary Cycles shader nodes. When the selector is a
constant, the kept code simply forwards the chosen branch. When it varies over the surface (for example it
comes from an Attribute or Object Info node), the selection has to be computed by shader nodes at render
time; that is the part to rewrite.

Blender has the same nodes in its shader editor (Blender 5.x); this lowering is meant to give the same
values as Blender's Cycles for the same graph. Manual (Attributes reference, Data Conversion,
https://docs.blender.org/manual/en/latest/modeling/geometry_nodes/attributes_reference.html, manual 5.3,
read 21 November 2026): "Between float and boolean - values greater than 0 are true".

## 2. Behaviour (mathematics)

Zero of a type: 0 (float, integer), false (boolean), (0, 0, 0) (vector, color), and for shaders a black
closure (the kept helper `constant(zero, "closure")` makes an Emission of strength 0).

Boolean value B(s) of a selector s **[interop]**:

- boolean: s itself;
- float or integer: s > 0 (so 0 and negative values are false; NaN is false);
- vector, point, normal: true when the vector is not (0, 0, 0) (equivalently its length is > 0);
- color: luminance(s) > 0, using the luminance that Cycles applies when a colour is connected to a float
  input (the engine's colour-management coefficients). A constant colour is therefore not decided at
  lowering time but in the graph.

Results:

- **Switch**: True input when B(selector), else False input.
- **Enable Output**: its Value input when B(enable), else the zero of the type.
- **Index Switch** with `count` items: let n = trunc(index) (rounding toward zero, so -0.9 gives 0 and
  -1.1 gives -1) **[interop]**. The result is item n when 0 <= n < count, and the zero of the type
  otherwise (negative or too large) **[interop]**. The index is not passed through B; it is used as a
  number.

## 3. Interface (keep it)

All three live in `src/core/shader_switches.cpp`; the call sites in `lower_shader_switches` stay.

- `std::optional<ShaderValue> boolean_constant(const ShaderValue &value, const std::string &source_type)`
  (free function in the anonymous namespace). Called when a Boolean selector input is connected to a
  node whose value `shader_constant_value` could fold. `value` holds `bool`, `int`, `float` or
  `std::array<float, 3>`; `source_type` is the catalogue type of the source node's output (`"float"`,
  `"int"`, `"boolean"`, `"vector"`, `"point"`, `"normal"`, `"color"`, ...) or `""` when unknown.
  Return `ShaderValue{bool}` with B(value), or `std::nullopt` when it cannot be decided here (a
  three-component value that is not a vector, point or normal: colour or unknown); throw
  `std::invalid_argument("Boolean selector requires numeric input")` for anything else.
- `boolean_endpoint` (lambda with signature `ShaderEndpoint(const ShaderEndpoint &endpoint, const
  std::string &source_type)`): called for a non-constant Boolean selector whose source is not already
  boolean. Return an endpoint of new nodes whose value is 1 for B true and 0 for false (it is used as a
  factor and as a boolean).
- `lower_varying` (lambda with signature `ShaderEndpoint(const Kind &kind, const Value &selector, int
  count, const std::function<Value(int)> &select)`): builds the selection for a non-constant selector.
  `kind.index` is true for Index Switch, `kind.enable` for Enable Output, `kind.type` is one of `"float"`,
  `"int"`, `"boolean"`, `"vector"`, `"color"`, `"closure"`. For Switch and Enable Output, `selector` has
  already been passed through `boolean_endpoint` (or is a boolean endpoint), `count` is 2, `select(0)` is
  the False input (Enable Output: the zero value) and `select(1)` the True input (Value). For Index Switch,
  `selector` is the raw index and 1 <= `count` <= 9998 (zero items is handled before). `select(i)` returns
  `Value` = `std::variant<ShaderValue, ShaderEndpoint>`: a constant or a link. Return the endpoint that
  carries the result, of the type of `kind.type`.

Tools available inside `lower_shader_switches` (kept): `append(definition, parameters)` adds a node and
returns its id (it enforces the 256-node budget and throws `std::invalid_argument` past it); `bind(value,
node_id, input_socket, socket_type)` connects a `Value` to an input (link, or parameter, or a constant
node for closures); `constant(value, type)` makes a constant node and returns its endpoint; `zero(type)`.

The result must be a graph of Cycles shader nodes from the catalogue. Useful ones, with the socket names
our catalogue uses: `cycles/math/1` (`math_type`, inputs `value1`, `value2`, `value3`, output `value`;
operations include `add`, `subtract`, `multiply`, `greater_than`, `less_than`, `compare`, `trunc`,
`floor`, `minimum`, `maximum`), `cycles/vector_math/1` (`vector1`, `vector2`; outputs `vector`, `value`;
`length`, `add`, `scale`, ...), `cycles/mix_float/1`, `cycles/mix_vector/1`, `cycles/mix_color/1` (`fac`,
`a`, `b`, `use_clamp`; output `result`), `cycles/mix_closure/1` (`fac`, `closure1`, `closure2`; output
`closure`), `cycles/add_closure/1`, `cycles/integer_math/1`, `cycles/boolean_math/1`, `cycles/value/1`,
`cycles/color/1`. Integer and boolean branches may be carried as floats (Cycles converts at the sockets);
integers above 2^24 then lose precision. That limit is what the node help text in
`src/houdini/material_vop.cpp` (lines 100-130) describes; if your construction keeps integers exact, say
so in the change, so that the help text is updated with it.

## 4. The construction is free

Build the selection in a way of your own. You are not asked to, and should not, reproduce how any other
program builds it. Two general approaches that meet the tests (both checked, section 5):

- **Masks and sum**: for each item, a 0/1 mask "n equals i" built from comparisons of n with the
  thresholds i - 1/2 and i + 1/2; the result is the sum over items of mask times item (for vectors and
  colours with vector math; out-of-range indices give all-zero masks and so the zero value).
- **Ascending thresholds**: start from the zero value; for i = 0, 1, ..., replace the running result by
  item i where n >= i (a comparison used as a Mix factor); finally replace it by zero where n >= count.

Other constructions (nested binary comparisons, a balanced tree) are equally fine. Constraints from the
tests (section 5): float lowering must use only the operations the structural test can evaluate, and a
shader Index Switch with 2 items must contain exactly 3 Mix Shader nodes; the second approach meets both
directly (for closures it uses count + 1 Mix Shader nodes and no Add Shader).

## 5. Reference tests

These tests judge the replacement. **They must stay as they are.**

- `tests/shader_switches_test.cpp` (catalogue section `manual`; standalone C++ with no Cycles library).
  It lowers small graphs and runs them with its own evaluator, which understands only: `cycles/value/1`,
  `cycles/attribute/1` (the varying input), `cycles/integer_math/1` (as value1 + value2),
  `cycles/boolean_math/1` (as OR), `cycles/math/1` with `greater_than`, `trunc` and `add`, and
  `cycles/mix_float/1` (factor clamped to [0, 1], default 0.5) **[test]**. Checks: static selection keeps
  int32 exactness (16777217) and prunes the unused branch; varying Switch equals "x > 0 ? True : False" for
  x in {-2, -0.001, 0, 0.001, 0.5, 1, 2} within 1e-9; varying Index Switch with items (0.2, 0.4, 0.8)
  equals item[trunc(x)] or 0 for x in {-3.4, -1.1, -0.9, 0, 0.4, 0.9, 1, 1.9, 2, 2.9, 3, 4} within 1e-7;
  constant index 4096 of 4097 items; out-of-range constant index gives 0 and one diagnostic; zero items;
  9999 items rejected; a varying Index Switch of 128 items must exceed the node budget and be rejected
  **[test: the construction needs more than about two nodes per item]**; a shader Index Switch with 2 items
  contains exactly 3 `cycles/mix_closure/1` nodes **[test]**; AOV writers and Enable Output/Warning
  cases (constant, kept code). The source sets `shader_definitions()` to empty, so `source_type` is `""`
  there: an unknown type must be treated as a number ("> 0").
  Run on Linux or Windows with any C++20 compiler (no Houdini, no Cycles):

  ```
  g++ -std=c++20 -Iinclude -Isrc -o /tmp/shader_switches_test tests/shader_switches_test.cpp src/core/shader_switches.cpp
  /tmp/shader_switches_test
  ```

  Expected output: `{"result":"passed","checks":37,...}`. Measured 21 November 2026 with g++ 11.4 on
  Linux: passes at `a777b6c` (37 checks); aborts with the removal message at the stub commit.
- `tests/houdini_blender53_utilities_smoke.py` (catalogue `scheduled`; hython, renders; tolerance
  `atol = 2e-5`): constant selectors (int32 exact, pruning a broken texture), a varying vector selector
  (Object Info location + (1, -1, 0), length > 0, gives True = 0.75), and a varying Index Switch of 5 items
  (0.1 ... 0.5) at index -2 -> 0, -0.9 -> 0.1, 0.9 -> 0.1, 1.9 -> 0.2, 4.9 -> 0.5, 5.1 -> 0; Enable Output.
  Needs the Windows build:

  ```powershell
  ./scripts/run-hython.ps1 tests/houdini_blender53_utilities_smoke.py --device CPU
  ```

- `tests/houdini_material_vop_smoke.py`, `tests/houdini_shader_input_states_smoke.py` (headless; node
  layout and input states of the switch nodes; Windows build).

Check of this document without a Windows build: a throw-away Python script (not committed) evaluated the
two constructions of section 4 with exactly the operations the structural test understands; both give
the expected values for every index of both tests (0 mismatches), and "x > 0" as a Mix factor gives the
Switch values. The renders were not checked (they need the Windows build).

## 6. Interoperability and test facts

| # | Fact | Pinned by |
|---|---|---|
| S1 | Number to Boolean is "> 0" (0 false) | structural test (x = 0, -0.001); manual |
| S2 | Vector to Boolean is "not the zero vector" | utilities smoke (vector selector) |
| S3 | Colour to Boolean goes through Cycles' colour-to-float luminance, decided in the graph | not pinned by a test |
| S4 | Index is truncated toward zero; -0.9 selects item 0 | both tests |
| S5 | Out-of-range index (negative or >= count) gives the zero of the type | both tests |
| S6 | Float lowering uses only value, math add/greater_than/trunc, mix_float, integer_math, boolean_math | structural test evaluator [test] |
| S7 | Shader Index Switch with 2 items uses exactly 3 Mix Shader nodes | structural test [test] |
| S8 | A varying Index Switch of 128 items exceeds the 256-node budget | structural test [test] |

## 7. Provenance

- The previous implementation (the Boolean conversion of selectors and the graph built for non-constant
  Switch, Enable Output and Index Switch selectors in `src/core/shader_switches.cpp`, lines 236-268, 286-297
  and 424-440 at `a777b6c`) followed Blender's GPL-2.0-or-later
  `source/blender/nodes/intern/shader_nodes_inline.cc` (v5.3.0) closely: the same construction for the
  index chain and the same conversions. The author of this description found it in a search for such code on
  21 November 2026.
- It was removed in commit `2e368d45064344551ac28f5c36c8333f33f8ea0c` (title "Remove
  Blender-derived non-constant switch lowering pending independent rewrite"). The hash is that of the
  private development branch; in a repository with new history it names the commit that removed the code.
- Commit hashes in this section are those of the private branch that integrated the removal and the rewrite on top of the main line (the side branches named here had other hashes for the same commits): removal `2e368d4`, base of the rewrite `8a6467b`, replacement `9dc5abb`.
- This description was written by an author who had read both the previous implementation and
  Blender's source. It contains no Blender code and no Blender internal names, but its author was not
  isolated from that code. This is not a clean-room process in the legal sense; provenance and code review
  still matter. Note: two structural test facts (S7, S8) were written for the previous construction and
  constrain the new one; the second construction of section 4 meets them, but it is shaped like a chain of
  count + 1 Mix nodes. The reviewer should judge whether that is acceptable or whether S7 should be relaxed.
- The replacement was written from this description by an implementer on 21 November 2026, on a separate
  branch from base `8a6467b`. What the implementer used: this document; the stubbed
  `src/core/shader_switches.cpp` and `tests/shader_switches_test.cpp` at the base commit; parts of our own
  `src/core/shader_graph.cpp` (supported node list, parameter setting, link type compatibility); the Apache-2.0
  Cycles node definitions in `third_party/cycles/src/scene/shader_nodes.cpp` (socket names and types of the
  Mix, Math, Boolean Math and Integer Math nodes). What the implementer did not look at: Blender's source outside
  `intern/cycles`, the removed implementation (the git working copy was cloned without checkout and switched
  straight to the stub base; no `git show`, `log -p`, `diff` or `blame` of earlier versions of the file), other
  working copies, and the old file in the main checkout. The implementer was not isolated from general knowledge of how
  such lowerings are usually built; this is not a legal clean room.
  Method: the Switch and Enable Output selection is a single blend (Mix Float / Vector / Color / Shader) with the
  0/1 selector as factor. The Index Switch is a balanced binary decision tree (binary search over a sorted
  table; Knuth, The Art of Computer Programming vol. 3, section 6.2.1) over n = trunc(index), with
  count + 2 leaves: zero, item 0 ... item count-1, zero. A subtree over leaves [lo, hi) splits at
  mid = (lo + hi) / 2 with the factor "n > mid - 1.5" (that is n >= mid - 1, exact because n is whole), so each
  n reaches exactly one leaf: item[n] inside the range, zero below and above, and also for NaN and infinities.
  Cost: 1 trunc, count + 1 comparisons and count + 1 blends (2 * count + 3 nodes; depth about
  log2(count + 2)); a 2-item shader Index Switch therefore has 3 Mix Shader nodes (S7) and the largest varying
  float Index Switch that fits the 256-node budget with one source node has 126 items (S8 holds at 128).
  Boolean conversion: numbers "> 0"; vectors, points and normals "length > 0" (constant and graph paths use the
  same test, so they agree also for NaN or underflowing components); colours and unknown triples are linked
  into a float "> 0" so that Cycles' colour-to-float conversion decides (S3). Integers and Booleans travel as
  floats, so integers above 2^24 still lose precision in a varying selection; the help text in
  `src/houdini/material_vop.cpp` stays correct. The structural test was not changed.
- Review against Blender's source for copied structure or expression: a separate reviewer, 21 November
  2026, compared with Blender's `source/blender/nodes/intern/shader_nodes_inline.cc` at `v5.3.0` and
  with the removed implementation. Grade: no concern. Details:
  `docs/dev/provenance/review-2026-11-21.md`, section 7. No follow-up was needed.

## Performance (21 November 2026)

Measured against the previous implementation (commit `03c6abf`, run as a black box, its source not read).
Node counts (Linux, g++ 11.4, probe `switch_counts.cpp`, varying selector): the previous construction used
2 * count + 2 nodes for float, colour and vector items; the rewrite as integrated used 2 * count + 3 (an extra
trunc). For shader items both used 2 * count + 4 nodes, but Cycles' SVM skips the unused branch of a Mix Shader,
so what runs per shading sample is about count nodes for the previous chain (64 items: 99 of 132 on average)
and about log2(count) for the balanced tree (64 items: 14). Render time, Windows, Ryzen 9 7950X CPU,
1024 x 1024, 64 samples, emission plane, 5 timed runs each (median): 32-item shader Index Switch 1.49 s old,
1.09 s rewrite; 8-item 1.10 s / 1.03 s; 8- and 32-item colour Index Switch equal (1.28 / 1.29 s, 2.10 / 2.10 s).
OptiX on the RTX 4090 (1024 samples): 32-item shader 1.18 s old, 1.06 s rewrite; colour within run-to-run drift.
Changes made after this measurement, both still the behaviour of section 2:
1. The tree's comparisons test the index x itself instead of trunc(x): trunc(x) >= k is x > (largest 32-bit
   float below k) for k >= 1 and x > k - 1 for k <= 0 (checked exhaustively for k in -10000 ... 10000 around
   every threshold, 9.7 million cases, 0 mismatches). One node fewer: value types back to 2 * count + 2.
2. A colour or vector Index Switch whose items are all finite constants (up to 255 items) becomes trunc, one
   multiply-add and an owned Color Ramp table with constant interpolation (3 nodes for any count): table entry 0
   and entries above count are zero, entry i + 1 holds item i, and the factor (trunc(x) + 1.5) / 256 makes the
   engine's lookup int(clamp(f, 0, 1) * 256) pick entry trunc(x) + 1 inside the range and a zero entry outside
   it (NaN gives entry 0). Both steps are exact in 32-bit floats. Float items keep the tree (the structural
   test evaluates only the nodes of fact S6). The structural test passes unchanged (37 checks); S7 and S8 hold.
After both changes (same scenes, Windows): CPU 8-item colour 1.28 -> 1.00 s, 32-item colour 2.10 -> 1.01 s
(previous implementation -> now), 32-item shader 1.49 -> 1.13 s; OptiX (old build re-run next to it, because
the GPU times drift by about 5 % between jobs) 32-item colour 1.37 -> 1.13 s, 32-item shader 1.24 -> 1.11 s,
8-item scenes within the drift. Images identical to the previous implementation's (every 4th pixel compared,
maximum difference 0). `houdini_blender53_utilities_smoke.py` (CPU), `houdini_material_vop_smoke.py` and
`houdini_shader_input_states_smoke.py` pass with the changed build.
