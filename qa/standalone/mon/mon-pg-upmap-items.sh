#!/usr/bin/env bash

source $CEPH_ROOT/qa/standalone/ceph-helpers.sh

function run() {
    local dir=$1
    shift

    export CEPH_MON="127.0.0.1:7148" # git grep '\<7148\>' : there must be only one
    export CEPH_ARGS
    CEPH_ARGS+="--fsid=$(uuidgen) --auth-supported=none "
    CEPH_ARGS+="--mon-host=$CEPH_MON "

    local funcs=${@:-$(set | sed -n -e 's/^\(TEST_[0-9a-z_]*\) .*/\1/p')}
    for func in $funcs ; do
        setup $dir || return 1
        $func $dir || return 1
        teardown $dir || return 1
    done
}

NUM_OSDS=4
POOL_NAME=upmap-pool

function setup_cluster() {
    local dir=$1
    run_mon $dir a || return 1
    run_mgr $dir x || return 1
    local i
    for ((i = 0; i < NUM_OSDS; i++)); do
        run_osd $dir $i || return 1
    done
    # prevent the balancer from rewriting mappings under test
    ceph balancer off || return 1
    create_pool $POOL_NAME 8 || return 1
    ceph osd pool set $POOL_NAME size 2 || return 1
    wait_for_clean || return 1
}

function get_pool_id() {
    ceph osd pool ls detail -f json | \
        jq ".[] | select(.pool_name == \"$POOL_NAME\") | .pool_id"
}

function up_primary_of() {
    local pgid=$1
    ceph pg map $pgid -f json | jq -r '.up[0]'
}

function target_for() {
    # first osd not in the pg's up set
    local pgid=$1
    local up
    up=$(ceph pg map $pgid -f json | jq -r '.up[]') || return 1
    local i
    for ((i = 0; i < NUM_OSDS; i++)); do
        if ! grep -qw $i <<< "$up" ; then
            echo $i
            return 0
        fi
    done
    return 1
}

function has_upmap() {
    local pgid=$1
    ceph osd dump -f json | \
        jq -e ".pg_upmap_items | any(.pgid == \"$pgid\")" > /dev/null
}

function TEST_pg_upmap_items_batch() {
    local dir=$1
    setup_cluster $dir || return 1

    local poolid=$(get_pool_id)
    local pg1="$poolid.0" pg2="$poolid.1" pg3="$poolid.2"
    local from1=$(up_primary_of $pg1) to1=$(target_for $pg1)
    local from2=$(up_primary_of $pg2) to2=$(target_for $pg2)
    local from3=$(up_primary_of $pg3) to3=$(target_for $pg3)

    # one command parses and applies mappings for multiple pgs
    ceph osd pg-upmap-items $pg1 $from1 $to1 $pg2 $from2 $to2 || return 1
    has_upmap $pg1 || return 1
    has_upmap $pg2 || return 1

    # a single bad group rejects the whole batch without applying the valid pg
    ! ceph osd pg-upmap-items $pg3 $from3 $to3 999.0 0 1 || return 1
    ! has_upmap $pg3 || return 1

    # duplicate pgid in one batch is rejected
    ! ceph osd pg-upmap-items $pg3 $from3 $to3 $pg3 $from3 $to3 || return 1
    # malformed pgid (trailing garbage) is rejected
    ! ceph osd pg-upmap-items ${pg3}junk $from3 $to3 || return 1
    # out-of-range osd id is rejected
    ! ceph osd pg-upmap-items $pg3 $from3 4294967296 || return 1
    ! has_upmap $pg3 || return 1

    # legacy named CLI arguments remain accepted
    ceph osd pg-upmap-items --pgid $pg3 --id $from3 --id $to3 || return 1
    has_upmap $pg3 || return 1
    ceph osd rm-pg-upmap-items $pg3 || return 1
    ! has_upmap $pg3 || return 1

    # one command removes mappings for multiple pgs
    ceph osd rm-pg-upmap-items $pg1 $pg2 || return 1
    ! has_upmap $pg1 || return 1
    ! has_upmap $pg2 || return 1
    # duplicate pgid is rejected
    ! ceph osd rm-pg-upmap-items $pg3 $pg3 || return 1
}

main mon-pg-upmap-items "$@"
