// -*- mode:C++; tab-width:8; c-basic-offset:2; indent-tabs-mode:t -*-
// vim: ts=8 sw=2 smarttab

#include <gtest/gtest.h>

#include <limits>

#include "crush/crush.h"
#include "mon/OSDMonitor.h"

using namespace std;

using groups_t = vector<pair<pg_t,vector<int64_t>>>;

static int parse(const vector<string>& args, groups_t& groups)
{
  stringstream ss;
  return OSDMonitor::parse_pg_upmap_items_args(args, groups, ss);
}

TEST(ParsePgidToken, Valid)
{
  pg_t pgid;
  ASSERT_TRUE(OSDMonitor::parse_pgid_token("1.0", pgid));
  ASSERT_EQ(pg_t(0, 1), pgid);
  ASSERT_TRUE(OSDMonitor::parse_pgid_token("12.7f", pgid));
  ASSERT_EQ(pg_t(0x7f, 12), pgid);
  ASSERT_TRUE(OSDMonitor::parse_pgid_token(
      "18446744073709551615.ffffffff", pgid));
  ASSERT_EQ(numeric_limits<uint64_t>::max(), pgid.m_pool);
  ASSERT_EQ(numeric_limits<ps_t>::max(), pgid.ps());
}

TEST(ParsePgidToken, Invalid)
{
  pg_t pgid;
  // pg_t::parse() accepts trailing garbage; the token parser must not
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1.7junk", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1.2.3", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("junk1.7", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1x.7", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("-1.7", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1.", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token(".7", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token("1.100000000", pgid));
  ASSERT_FALSE(OSDMonitor::parse_pgid_token(
      "18446744073709551616.0", pgid));
}

TEST(ParsePgUpmapItemsArgs, SinglePg)
{
  groups_t groups;
  ASSERT_EQ(0, parse({"1.0", "0", "1"}, groups));
  ASSERT_EQ(1u, groups.size());
  ASSERT_EQ(pg_t(0, 1), groups[0].first);
  ASSERT_EQ((vector<int64_t>{0, 1}), groups[0].second);
}

TEST(ParsePgUpmapItemsArgs, MultiplePgs)
{
  groups_t groups;
  ASSERT_EQ(0, parse({"1.0", "0", "1", "2", "3",
		      "1.1", "osd.4", "5"}, groups));
  ASSERT_EQ(2u, groups.size());
  ASSERT_EQ(pg_t(0, 1), groups[0].first);
  ASSERT_EQ((vector<int64_t>{0, 1, 2, 3}), groups[0].second);
  ASSERT_EQ(pg_t(1, 1), groups[1].first);
  ASSERT_EQ((vector<int64_t>{4, 5}), groups[1].second);
}

TEST(ParsePgUpmapItemsArgs, CrushItemNoneTarget)
{
  groups_t groups;
  ASSERT_EQ(0, parse({"1.0", "0", std::to_string(CRUSH_ITEM_NONE)}, groups));
  ASSERT_EQ((vector<int64_t>{0, CRUSH_ITEM_NONE}), groups[0].second);
}

TEST(ParsePgUpmapItemsArgs, RejectsEmptyArgs)
{
  groups_t groups;
  ASSERT_EQ(-EINVAL, parse({}, groups));
}

TEST(ParsePgUpmapItemsArgs, RejectsDuplicatePg)
{
  groups_t groups;
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "1", "1.0", "2", "3"}, groups));
}

TEST(ParsePgUpmapItemsArgs, RejectsOddIdCount)
{
  groups_t groups;
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "1", "2"}, groups));
}

TEST(ParsePgUpmapItemsArgs, RejectsPgWithoutIds)
{
  groups_t groups;
  ASSERT_EQ(-EINVAL, parse({"1.0", "1.1", "0", "1"}, groups));
  groups.clear();
  ASSERT_EQ(-EINVAL, parse({"1.0"}, groups));
}

TEST(ParsePgUpmapItemsArgs, RejectsMalformedTokens)
{
  groups_t groups;
  // a malformed pgid must not be misparsed as its "1.7" prefix
  ASSERT_EQ(-EINVAL, parse({"1.7junk", "0", "1"}, groups));
  groups.clear();
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "1", "1.5junk", "2", "3"}, groups));
  groups.clear();
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "junk"}, groups));
}

TEST(ParsePgUpmapItemsArgs, RejectsOutOfRangeOsdIds)
{
  groups_t groups;
  // > 0x7fffffff would wrap when narrowed to a 32-bit osd id
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "4294967296"}, groups));
  groups.clear();
  ASSERT_EQ(-EINVAL, parse({"1.0", "0", "2147483648"}, groups));
  groups.clear();
  ASSERT_EQ(-EINVAL, parse({"1.0", "-1", "1"}, groups));
}
