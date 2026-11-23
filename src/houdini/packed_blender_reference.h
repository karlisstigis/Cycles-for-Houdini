/*
 * Copyright (c) 2026
 *	Side Effects Software Inc.  All rights reserved.
 *
 * Redistribution and use of Houdini Development Kit samples in source and
 * binary forms, with or without modification, are permitted provided that the
 * following conditions are met:
 * 1. Redistributions of source code must retain the above copyright notice,
 *    this list of conditions and the following disclaimer.
 * 2. The name of Side Effects Software may not be used to endorse or
 *    promote products derived from this software without specific prior
 *    written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY SIDE EFFECTS SOFTWARE `AS IS' AND ANY EXPRESS
 * OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
 * OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.  IN
 * NO EVENT SHALL SIDE EFFECTS SOFTWARE BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
 * OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
 * LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
 * NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
 * EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 *----------------------------------------------------------------------------
 */

// Registration and HDK interface based on the installed HDK samples.
// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <GU/GU_PackedImpl.h>
#include <UT/UT_Options.h>
#include <memory>
#include <string>

namespace cycles_houdini {
struct BlenderReferenceBounds;
struct BlenderReferenceBoundsLease;
struct BlenderReferenceHull;
struct BlenderReferencePreviewLease;
// Serialized asset reference only. None of these callbacks evaluates Blender.
class PackedBlenderReference final : public GU_PackedImpl {
public:
  static constexpr const char *typeName = "CyclesBlenderReference";
  static void install(GA_PrimitiveFactory *factory);
  static const GA_PrimitiveTypeId &typeId();
  GU_PackedFactory *getFactory() const override;
  GU_PackedImpl *copy() const override;
  void clearData() override {}
  bool isValid() const override;
  bool load(GU_PrimPacked *, const UT_Options &, const GA_LoadMap &) override;
  bool save(UT_Options &, const GA_SaveMap &) const override;
  void update(GU_PrimPacked *, const UT_Options &) override;
  bool getBounds(UT_BoundingBox &) const override;
  bool getRenderingBounds(UT_BoundingBox &) const override;
  void getVelocityRange(UT_Vector3 &min, UT_Vector3 &max) const override { min = max = 0; }
  void getWidthRange(fpreal &min, fpreal &max) const override { min = max = 0; }
  bool unpack(GU_Detail &, const UT_Matrix4D *) const override { return false; }
  GU_ConstDetailHandle getPackedDetail(GU_PackedContext * = nullptr) const override;
  int64 getMemoryUsage(bool inclusive) const override;
  void countMemory(UT_MemoryCounter &, bool inclusive) const override;

  const std::string &file() const { return file_; }
  const std::string &target() const { return target_; }
  const std::string &targetKind() const { return kind_; }
  fpreal sourceFrame() const { return frame_; }
  std::string reference() const;
  std::string intrinsicReference(const GU_PrimPacked *) const { return reference(); }
  const char *intrinsicFile(const GU_PrimPacked *) const { return file_.c_str(); }
  const char *intrinsicTarget(const GU_PrimPacked *) const { return target_.c_str(); }
  const char *intrinsicKind(const GU_PrimPacked *) const { return kind_.c_str(); }
  fpreal intrinsicFrame(const GU_PrimPacked *) const { return frame_; }
  fpreal intrinsicShapeFrame(const GU_PrimPacked *) const { return shape_frame_; }
  bool intrinsicViewportWireframe(const GU_PrimPacked *) const { return viewport_wireframe_; }
  std::string intrinsicShapeBounds(const GU_PrimPacked *) const;
  int64 intrinsicViewportDetailId(const GU_PrimPacked *) const;
  bool intrinsicBoundsKnown(const GU_PrimPacked *) const;
  std::string intrinsicBounds(const GU_PrimPacked *) const;
  const char *intrinsicBoundsStatus(const GU_PrimPacked *) const;
  const char *intrinsicViewportShape(const GU_PrimPacked *) const { return viewport_shape_.c_str(); }
  std::string intrinsicViewportHull(const GU_PrimPacked *) const;
  bool intrinsicHullPerMember(const GU_PrimPacked *) const { return hull_per_member_; }
  int64 intrinsicHullPoints(const GU_PrimPacked *) const;
  int64 intrinsicHullTriangles(const GU_PrimPacked *) const;
  void setViewportShape(GU_PrimPacked *, const char *);
  void setViewportHull(GU_PrimPacked *, const std::string &);
  void setFile(GU_PrimPacked *, const char *);
  void setTarget(GU_PrimPacked *, const char *);
  void setKind(GU_PrimPacked *, const char *);
  void setFrame(GU_PrimPacked *, fpreal);
  void setShapeFrame(GU_PrimPacked *, fpreal);
  void setShapeBounds(GU_PrimPacked *, const std::string &);
  void setViewportWireframe(GU_PrimPacked *, bool);
  void setBounds(GU_PrimPacked *, const std::string &);
  void setPreviewBounds(GU_PrimPacked *, const std::string &);

private:
  void dirty(GU_PrimPacked *, bool invalidate = true);
  bool knownBounds(UT_BoundingBox &) const;
  bool viewportPreviewValid() const;
  void attachPreviewLease(const std::string &);
  void rebuildViewportDetail();
  std::shared_ptr<const BlenderReferenceBoundsLease> retainedBounds() const;
  std::string file_, target_, kind_{"object"};
  fpreal frame_{1};
  fpreal shape_frame_{1};
  std::shared_ptr<const BlenderReferenceBoundsLease> bounds_;
  std::shared_ptr<const BlenderReferenceBounds> preview_;
  std::shared_ptr<const BlenderReferenceBounds> shape_bounds_;
  std::string viewport_shape_{"bounds"};
  bool hull_per_member_ = false;
  bool viewport_wireframe_ = true;
  std::shared_ptr<const BlenderReferenceHull> hull_;
  GU_ConstDetailHandle viewport_detail_;
  std::shared_ptr<const BlenderReferencePreviewLease> preview_lease_;
  GU_ConstDetailHandle fallback_detail_;
};
const PackedBlenderReference *packed_blender_reference(const GU_PrimPacked &) noexcept;
} // namespace cycles_houdini
