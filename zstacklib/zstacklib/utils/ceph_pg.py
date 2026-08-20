# Hammer rejects pg_num 0; Infernalis and later resolve it to Ceph's default.
CEPH_DEFAULT_PG_NUM_MIN_VERSION = (9, 2)


def _get_major_minor_version(ceph_version):
    return tuple(int(part) for part in ceph_version.split('.', 2)[:2])


def get_compatible_pg_num_for_pool_create(ceph_version):
    if _get_major_minor_version(ceph_version) < CEPH_DEFAULT_PG_NUM_MIN_VERSION:
        return 128
    return 0


def create_pools_with_compatible_pg_num(shell_call, pool_names, ceph_version):
    if not pool_names:
        return

    pg_num = get_compatible_pg_num_for_pool_create(ceph_version)
    for pool_name in pool_names:
        shell_call('ceph osd pool create %s %s' % (pool_name, pg_num))
