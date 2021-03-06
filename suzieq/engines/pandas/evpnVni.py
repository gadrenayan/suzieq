import pandas as pd
import ipaddress

from .engineobj import SqEngineObject
from suzieq.sqobjects.macs import MacsObj
from suzieq.sqobjects.interfaces import IfObj
from suzieq.sqobjects.routes import RoutesObj


class EvpnvniObj(SqEngineObject):
    pass

    def summarize(self, **kwargs):
        self._init_summarize(self.iobj._table, **kwargs)
        if self.summary_df.empty:
            return self.summary_df

        self._summarize_on_add_field = [
            ('deviceCnt', 'hostname', 'nunique'),
        ]
        self._summarize_on_add_list_or_count = [
            ('uniqueVniCnt', 'vni'), ('UniqueVniTypeCnt', 'type'),
            ('uniqueVrfCnt', 'vrf')
        ]

        self._summarize_on_add_stat = [
            ('macsInVniStat', '', 'numMacs'),
            ('arpNdInVniStat', '', 'numArpNd')
        ]

        self._gen_summarize_data()

        # To summarize accurately, we need to explode the remoteVteps
        # column from a list to an individual entry for each
        # remoteVteps in that list. The resulting set can be huge if them
        # number of Vteps times the ports is huge.
        #
        # the 'explode' only works post pandas 0.25

        self.summary_df = self.summary_df.explode(
            'remoteVtepList').dropna(how='any')
        self.nsgrp = self.summary_df.groupby(by=["namespace"])

        if not self.summary_df.empty:
            herPerVtepCnt = self.summary_df.groupby(
                by=['namespace', 'hostname'])['remoteVtepList'].count()
            self._add_stats_to_summary(herPerVtepCnt, 'herPerVtepStat',
                                       filter_by_ns=True)
        self.summary_row_order.append('herPerVtepStat')

        self._post_summarize(check_empty_col='deviceCnt')
        return self.ns_df.convert_dtypes()

    def aver(self, **kwargs) -> pd.DataFrame:
        """Assert for EVPN Data"""

        assert_cols = ["namespace", "hostname", "vni", "remoteVtepList", "vrf",
                       "type", "srcVtepIp", "state", "l2VniList", "ifname"]

        kwargs.pop("columns", None)  # Loose whatever's passed

        df = self.get(columns=assert_cols, **kwargs)
        if df.empty:
            return pd.DataFrame(columns=assert_cols)

        # Gather the unique set of VTEPs per VNI
        vteps_df = df.explode(column='remoteVtepList') \
                     .dropna(how='any') \
                     .groupby(by=['vni', 'type'])['remoteVtepList'] \
                     .aggregate(lambda x: x.unique().tolist()) \
                     .reset_index() \
                     .dropna(how='any') \
                     .rename(columns={'remoteVtepList': 'allVteps'})

        df = df.merge(vteps_df)

        df["assertReason"] = [[] for _ in range(len(df))]

        # Every VTEP has info about every other VTEP for a given VNI
        df["assertReason"] += df.apply(self._all_vteps_present, axis=1)

        # Every VTEP is reachable
        df["assertReason"] += df.apply(self._is_vtep_reachable, axis=1)

        # State is up
        df["assertReason"] += df.apply(
            lambda x: ['interface is down']
            if x['type'] == "L2" and x['state'] != "up"
            else [], axis=1)

        hosts = df["hostname"].unique().tolist()
        ifdf = IfObj(context=self.ctxt) \
            .get(namespace=kwargs.get("namespace", ""), hostname=hosts)

        df = df.merge(ifdf[['namespace', 'hostname', 'ifname', 'master',
                            'vlan']],
                      on=['namespace', 'hostname', 'ifname'], how='left')

        # vxlan interfaces for every VNI is part of bridge
        df["assertReason"] += df.apply(
            lambda x: ['vni not in bridge']
            if x['type'] == "L2" and x['master'] != "bridge" else [],
            axis=1)

        mac_df = MacsObj(context=self.ctxt) \
            .get(namespace=kwargs.get("namespace", ""),
                 macaddr=["00:00:00:00:00:00"])

        # Assert that we have HER for every remote VTEP
        df['assertReason'] += df.apply(self._is_her_good,
                                       args=(mac_df, ), axis=1)

        # Fill out the assert column
        df['assert'] = df.apply(lambda x: 'pass'
                                if not len(x.assertReason) else 'fail',
                                axis=1)

        return df[['namespace', 'hostname', 'vni', 'type',
                   'assertReason', 'assert', 'timestamp']].explode(column='assertReason') \
            .fillna({'assertReason': '-'})

    def _all_vteps_present(self, row):
        if row['type'] != "L2":
            return []

        if (set(row['remoteVtepList']).union(set([row['srcVtepIp']])) ==
                set(row['allVteps'])):
            return []

        return(['some remote VTEPs missing'])

    def _is_vtep_reachable(self, row):
        reason = []
        defrt = ipaddress.IPv4Network("0.0.0.0/0")
        for vtep in row['remoteVtepList'].tolist():
            rdf = RoutesObj(context=self.ctxt) \
                .lpm(namespace=row['namespace'], vrf=['default'],
                     hostname=row['hostname'], address=vtep)
            if rdf.empty:
                reason += [f"{vtep} not reachable"]
                continue
            if rdf.prefix[0] == defrt:
                reason += [f"{vtep} reachable via default"]

        return reason

    def _is_her_good(self, row, mac_df):
        reason = []

        if row['type'] == 'L3':
            return reason

        her_list = mac_df[(mac_df['hostname'] == row['hostname']) &
                          (mac_df['namespace'] == row['namespace']) &
                          (mac_df['oif'] == row['ifname'])]['remoteVtepIp'] \
            .tolist()
        missing_hers = set(row['remoteVtepList']).difference(set(her_list))
        if not missing_hers:
            return reason
        return [f'HER is missing VTEPs {missing_hers}']
