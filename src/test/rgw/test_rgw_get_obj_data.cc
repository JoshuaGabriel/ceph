// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:nil -*-
// vim: ts=8 sw=2 sts=2 expandtab ft=cpp

#include "rgw_rados.h"
#include "rgw_aio_throttle.h"
#include "rgw_sal.h"

#include <string>
#include <boost/asio/io_context.hpp>
#include <boost/asio/post.hpp>
#include <spawn/spawn.hpp>

#include <gtest/gtest.h>

namespace {

constexpr uint64_t chunk = 1024;
constexpr uint64_t window = 4 * chunk;
constexpr uint64_t total = 64 * chunk;

// each chunk's bytes identify it, so out-of-order delivery shows up
std::string chunk_data(uint64_t ofs, uint64_t len = chunk)
{
  return std::string(len, char('a' + (ofs / chunk) % 26));
}

struct CollectData : RGWGetDataCB {
  bufferlist received;
  int handle_data(bufferlist& bl, off_t bl_ofs, off_t bl_len) override {
    bufferlist part;
    part.substr_of(bl, bl_ofs, bl_len);
    received.claim_append(part);
    return 0;
  }
};

// reads a 'total' byte object through get_obj_data the way
// RGWRados::Object::Read::iterate() does. the read at offset 0 stays in
// flight until complete_first_read(), and every other read completes on the
// next turn of the io_context
struct SlowFirstRead {
  boost::asio::io_context context;
  RGWRados store; // only asked whether d3n is enabled
  CollectData cb;
  rgw::Aio* first_aio = nullptr;
  rgw::AioResult* first_result = nullptr;
  get_obj_data* data = nullptr; // valid while the reader is suspended
  uint64_t issued = 0;
  bool done = false;

  rgw::Aio::OpFunc hold_read() {
    return [this] (rgw::Aio* aio, rgw::AioResult& r) {
      first_aio = aio;
      first_result = &r;
    };
  }

  rgw::Aio::OpFunc fast_read(uint64_t ofs) {
    return [this, ofs] (rgw::Aio* aio, rgw::AioResult& r) {
      r.data.append(chunk_data(ofs));
      boost::asio::post(context, [aio, &r] { aio->put(r); });
    };
  }

  void start(uint64_t max_buffered) {
    spawn::spawn(context,
      [this, max_buffered] (yield_context yield) {
        rgw::YieldingAioThrottle aio(window, context, yield);
        get_obj_data d(&store, &cb, &aio, 0, max_buffered, null_yield);
        data = &d;
        const rgw_raw_obj obj{{"testpool"}, "obj"};
        for (uint64_t ofs = 0; ofs < total; ofs += chunk) {
          auto op = ofs == 0 ? hold_read() : fast_read(ofs);
          auto c = aio.get(obj, std::move(op), chunk, ofs);
          ++issued;
          EXPECT_EQ(0, d.flush(std::move(c)));
        }
        EXPECT_EQ(0, d.drain());
        data = nullptr;
        done = true;
      });
  }

  void complete_first_read(uint64_t len) {
    first_result->data.append(chunk_data(0, len));
    first_aio->put(*first_result);
  }
};

} // anonymous namespace

TEST(GetObjData, SlowReadBoundsBufferedData)
{
  constexpr uint64_t max_buffered = 8 * chunk;
  SlowFirstRead r;
  r.start(max_buffered);

  r.context.poll(); // run until blocked on the first read
  ASSERT_FALSE(r.done);
  // stopped issuing reads instead of buffering the rest of the object
  EXPECT_LT(r.issued, total / chunk);
  EXPECT_GE(r.data->completed_size, max_buffered);
  EXPECT_LT(r.data->completed_size, max_buffered + window);

  r.complete_first_read(chunk);
  r.context.run();
  EXPECT_TRUE(r.done);

  std::string expected;
  for (uint64_t ofs = 0; ofs < total; ofs += chunk) {
    expected += chunk_data(ofs);
  }
  EXPECT_EQ(total, r.cb.received.length());
  EXPECT_TRUE(r.cb.received.to_str() == expected);
}

TEST(GetObjData, NoLimitBuffersRestOfObject)
{
  SlowFirstRead r;
  r.start(0);

  r.context.poll(); // run until blocked on the first read
  ASSERT_FALSE(r.done);
  EXPECT_EQ(total / chunk, r.issued);
  EXPECT_EQ(total - chunk, r.data->completed_size);

  r.complete_first_read(chunk);
  r.context.run();
  EXPECT_TRUE(r.done);
  EXPECT_EQ(total, r.cb.received.length());
}

// a short first read leaves a gap at offset that no later read can fill.
// once nothing is in flight, flush() has to stop waiting instead of spinning
TEST(GetObjData, UnfillableGapDoesNotHang)
{
  SlowFirstRead r;
  r.start(8 * chunk);

  r.context.poll(); // run until blocked on the first read
  ASSERT_FALSE(r.done);

  r.complete_first_read(chunk / 2);
  r.context.run();
  EXPECT_TRUE(r.done);
  EXPECT_EQ(total / chunk, r.issued);
}
