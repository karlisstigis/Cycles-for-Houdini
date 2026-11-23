// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <initializer_list>
#include <memory>
#include <utility>
#include <type_traits>
#include <vector>

namespace cycles_houdini {
// Value storage for immutable scene fields. Exporting mutable access is sticky:
// subsequent copies clone, even if the exported reference is no longer tracked
// by its caller. This is stronger than ordinary copy-on-write, which cannot
// protect a snapshot from a reference acquired before it was copied.
template <class T> class SharedArray {
  using Vector = std::vector<T>;
  std::shared_ptr<Vector> storage_;
  bool exposed_{};
  static const Vector &empty_vector() {
    static const Vector value;
    return value;
  }
  const Vector &read() const { return storage_ ? *storage_ : empty_vector(); }
  Vector &write() {
    if (!storage_)
      storage_ = std::make_shared<Vector>();
    else if (storage_.use_count() != 1)
      storage_ = std::make_shared<Vector>(*storage_);
    return *storage_;
  }
  Vector &expose() {
    auto &value = write();
    exposed_ = true;
    return value;
  }

public:
  using value_type = T;
  using size_type = typename Vector::size_type;
  using difference_type = typename Vector::difference_type;
  using reference = typename Vector::reference;
  using const_reference = typename Vector::const_reference;
  using pointer = typename Vector::pointer;
  using const_pointer = typename Vector::const_pointer;
  using iterator = typename Vector::iterator;
  using const_iterator = typename Vector::const_iterator;
  using reverse_iterator = typename Vector::reverse_iterator;
  using const_reverse_iterator = typename Vector::const_reverse_iterator;
  SharedArray() = default;
  explicit SharedArray(size_type count) : storage_(std::make_shared<Vector>(count)) {}
  SharedArray(size_type count, const T &value) : storage_(std::make_shared<Vector>(count, value)) {}
  SharedArray(std::initializer_list<T> values) : storage_(std::make_shared<Vector>(values)) {}
  template <class It>
  SharedArray(It first, It last)
      : storage_(std::make_shared<Vector>(first, last)), exposed_(!std::is_trivially_copyable_v<T>) {}
  SharedArray(const Vector &value) : storage_(std::make_shared<Vector>(value)) {}
  // A vector move preserves its caller's references and iterators. Never
  // share that allocation into a later immutable snapshot without cloning.
  SharedArray(Vector &&value) : storage_(std::make_shared<Vector>(std::move(value))), exposed_(true) {}
  SharedArray(const SharedArray &value)
      : storage_(value.exposed_ && value.storage_ ? std::make_shared<Vector>(*value.storage_) : value.storage_) {}
  SharedArray(SharedArray &&value) noexcept
      : storage_(std::move(value.storage_)), exposed_(std::exchange(value.exposed_, false)) {}
  SharedArray &operator=(const SharedArray &value) {
    if (this != &value) {
      SharedArray copy(value);
      swap(copy);
    }
    return *this;
  }
  SharedArray &operator=(SharedArray &&value) noexcept {
    if (this != &value) {
      storage_ = std::move(value.storage_);
      exposed_ = std::exchange(value.exposed_, false);
    }
    return *this;
  }
  SharedArray &operator=(const Vector &value) {
    SharedArray copy(value);
    swap(copy);
    return *this;
  }
  SharedArray &operator=(Vector &&value) {
    SharedArray copy(std::move(value));
    swap(copy);
    return *this;
  }
  SharedArray &operator=(std::initializer_list<T> value) {
    assign(value);
    return *this;
  }
  operator const Vector &() const { return read(); }
  operator Vector &() { return expose(); }
  bool empty() const noexcept { return !storage_ || storage_->empty(); }
  size_type size() const noexcept { return storage_ ? storage_->size() : 0; }
  size_type capacity() const noexcept { return storage_ ? storage_->capacity() : 0; }
  size_type max_size() const noexcept { return read().max_size(); }
  const_pointer data() const noexcept { return read().data(); }
  pointer data() { return expose().data(); }
  const_reference operator[](size_type i) const { return read()[i]; }
  reference operator[](size_type i) { return expose()[i]; }
  const_reference at(size_type i) const { return read().at(i); }
  reference at(size_type i) { return expose().at(i); }
  const_reference front() const { return read().front(); }
  reference front() { return expose().front(); }
  const_reference back() const { return read().back(); }
  reference back() { return expose().back(); }
  const_iterator begin() const noexcept { return read().begin(); }
  const_iterator end() const noexcept { return read().end(); }
  const_iterator cbegin() const noexcept { return read().cbegin(); }
  const_iterator cend() const noexcept { return read().cend(); }
  iterator begin() { return expose().begin(); }
  iterator end() { return expose().end(); }
  const_reverse_iterator rbegin() const noexcept { return read().rbegin(); }
  const_reverse_iterator rend() const noexcept { return read().rend(); }
  reverse_iterator rbegin() { return expose().rbegin(); }
  reverse_iterator rend() { return expose().rend(); }
  void reserve(size_type n) {
    if (n > capacity())
      write().reserve(n);
  }
  void resize(size_type n) {
    if (n != size())
      write().resize(n);
  }
  void resize(size_type n, const T &value) {
    if (n != size())
      write().resize(n, value);
  }
  void shrink_to_fit() {
    if (storage_)
      write().shrink_to_fit();
  }
  void clear() {
    if (!empty())
      write().clear();
  }
  void push_back(const T &value) { write().push_back(value); }
  void push_back(T &&value) {
    // Moving a nested value container preserves references into its owned
    // elements. Its import must therefore be treated like mutable export.
    if constexpr (!std::is_trivially_copyable_v<T>)
      exposed_ = true;
    write().push_back(std::move(value));
  }
  template <class... A> reference emplace_back(A &&...args) { return expose().emplace_back(std::forward<A>(args)...); }
  void pop_back() { write().pop_back(); }
  void assign(size_type n, const T &value) { write().assign(n, value); }
  template <class It> void assign(It first, It last) {
    if constexpr (!std::is_trivially_copyable_v<T>)
      exposed_ = true;
    write().assign(first, last);
  }
  void assign(std::initializer_list<T> values) { write().assign(values); }
  iterator insert(const_iterator position, const T &value) {
    const auto i = position - cbegin();
    auto &v = expose();
    return v.insert(v.cbegin() + i, value);
  }
  iterator insert(const_iterator position, T &&value) {
    const auto i = position - cbegin();
    auto &v = expose();
    return v.insert(v.cbegin() + i, std::move(value));
  }
  iterator insert(const_iterator position, size_type n, const T &value) {
    const auto i = position - cbegin();
    auto &v = expose();
    return v.insert(v.cbegin() + i, n, value);
  }
  template <class It> iterator insert(const_iterator position, It first, It last) {
    const auto i = position - cbegin();
    auto &v = expose();
    return v.insert(v.cbegin() + i, first, last);
  }
  iterator erase(const_iterator position) {
    const auto i = position - cbegin();
    auto &v = expose();
    return v.erase(v.cbegin() + i);
  }
  iterator erase(const_iterator first, const_iterator last) {
    const auto i = first - cbegin(), j = last - cbegin();
    auto &v = expose();
    return v.erase(v.cbegin() + i, v.cbegin() + j);
  }
  void swap(SharedArray &value) noexcept {
    storage_.swap(value.storage_);
    std::swap(exposed_, value.exposed_);
  }
  // Inspectable ownership, never a mutable storage handle.
  const void *storage_identity() const noexcept { return storage_.get(); }
  bool mutable_access_exposed() const noexcept { return exposed_; }
  friend bool operator==(const SharedArray &a, const SharedArray &b) {
    return a.storage_ == b.storage_ || a.read() == b.read();
  }
  friend bool operator==(const SharedArray &a, const Vector &b) { return a.read() == b; }
  friend bool operator==(const Vector &a, const SharedArray &b) { return a == b.read(); }
};
} // namespace cycles_houdini
