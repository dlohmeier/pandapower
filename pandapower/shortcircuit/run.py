# -*- coding: utf-8 -*-

# Copyright (c) 2016 by University of Kassel and Fraunhofer Institute for Wind Energy and Energy
# System Technology (IWES), Kassel. All rights reserved. Use of this source code is governed by a
# BSD-style license that can be found in the LICENSE file.

import pandas as pd
import warnings
import numpy as np
from scipy.sparse.linalg import inv
from scipy.sparse import diags

from pandapower.shortcircuit.currents import calc_ikss, calc_ip, calc_ith
#from pandapower.shortcircuit.kappa import calc_kappa
from pandapower.powerflow import _add_auxiliary_elements
from pandapower.auxiliary import _select_is_elements, _clean_up, _add_ppc_options, _add_sc_options
from pandapower.pypower_extensions.makeYbus import makeYbus
from pandapower.pd2ppc import _pd2ppc

from pandapower.shortcircuit.idx_bus import *
from pandapower.shortcircuit.idx_brch import *
from pypower.idx_bus import BASE_KV
from pandapower.shortcircuit.kappa import _add_kappa_to_ppc, _add_c_to_ppc
from pandapower.results import _copy_results_ppci_to_ppc
try:
    import pplog as logging
except:
    import logging

logger = logging.getLogger(__name__)

def runsc(net, case='max', lv_tol_percent=10, network_structure="auto", ip=False, ith=False, 
          tk_s=1., r_fault_ohm=0., x_fault_ohm=0.):
    
    """
    Calculates minimal or maximal symmetrical short-circuit currents.  
    The calculation is based on the method of the equivalent voltage source
    according to DIN/IEC EN 60909.
    The initial short-circuit alternating current *ikss* is the basis of the short-circuit
    calculation and is therefore always calculated.
    Other short-circuit currents can be calculated from *ikss* with the conversion factors defined
    in DIN/IEC EN 60909.
    
    The output is stored in the net.res_bus_sc table as a short_circuit current
    for each bus.

    INPUT:
        **net** (pandapowerNet) pandapower Network
        
        **case** (str) 'max' / 'min' for maximal / minimal current calculation
        
        **lv_tol_percent** (int) voltage tolerance band in the low voltage grid,  can be either 6% or 10% according to IEC 60909
            
        **ip** (bool) if True, calculate aperiodic short-circuit current 
        
        **Ith** (bool) if True, calculate equivalent thermical short-circuit current Ith

        **meshing** (str) define option for meshing (only relevant for ip and ith)
        
            "meshed" - it is assumed all buses are supplied over multiple paths
            
            "radial" - it is assumed all buses are supplied over exactly one path
            
            "auto" - topology check for each bus is performed to see if it is supplied over multiple paths (might be computationally expensive)

        **tk_s** (float) failure clearing time in seconds (only relevant for ith)

    OUTPUT:
    
    EXAMPLE:
        runsc(net)

        print(net.res_bus_sc)
    """
    if ip and len(net.gen) > 0:
        raise NotImplementedError("aperiodic short-circuit current not implemented for short circuits close to generators")

    if ith and len(net.gen) > 0:
        raise NotImplementedError("thermical short-circuit current not implemented for short circuits close to generators")

    if case not in ['max', 'min']:
        raise ValueError('case can only be "min" or "max" for minimal or maximal short "\
                                "circuit current')
    if network_structure not in ["meshed", "radial", "auto"]:
        raise ValueError('specify network structure as "meshed", "radial" or "auto"')        
            
    if len(net.ext_grid) > 0:
        if  not "s_sc_%s_mva"%case in net.ext_grid or any(pd.isnull(net.ext_grid["s_sc_%s_mva"%case])):
            raise ValueError("s_sc_%s is not defined for all ext_grids" %case)
        if  not "rx_%s"%case in net.ext_grid or any(pd.isnull(net.ext_grid["rx_%s"%case])):
            raise ValueError("rx_%s is not defined for all ext_grids" %case)
    kappa = ith or ip
    net["_options"] = {}
    _add_ppc_options(net, calculate_voltage_angles=False, 
                             trafo_model="pi", check_connectivity=False,
                             mode="sc", copy_constraints_to_ppc=False,
                             r_switch=0.0, init="flat", enforce_q_lims=False)
    _add_sc_options(net, case=case, lv_tol_percent=lv_tol_percent, tk_s=tk_s, 
                    network_structure=network_structure, r_fault_ohm=r_fault_ohm, 
                    x_fault_ohm=x_fault_ohm, kappa=kappa, ip=ip, ith=ith)
    net["_is_elems"] = _select_is_elements(net, None)
    _add_auxiliary_elements(net)
    ppc, ppci = _pd2ppc(net)
    calc_equiv_sc_impedance(net, ppci)
    _add_kappa_to_ppc(net, ppci)
    calc_ikss(net, ppci)
    if ip:
        calc_ip(ppci)
    if ith:
        calc_ith(net, ppci)
    ppc = _copy_results_ppci_to_ppc(ppci, ppc, "sc")
    _extract_results(net, ppc)
    _clean_up(net)

       
def calc_equiv_sc_impedance(net, ppc):
    r_fault = net["_options"]["r_fault_ohm"]
    x_fault = net["_options"]["x_fault_ohm"]
    zbus = calc_zbus(ppc)
    if r_fault > 0 or x_fault > 0:
        base_r = np.square(ppc["bus"][:, BASE_KV]) / ppc["baseMVA"]
        fault = diags((r_fault + x_fault * 1j) / base_r)
        zbus += fault
    zbus = zbus.toarray()
    z_equiv = np.diag(zbus)
    ppc["bus_sc"][:, R_EQUIV] = z_equiv.real 
    ppc["bus_sc"][:, X_EQUIV] = z_equiv.imag
    ppc["internal"]["zbus"] = zbus

def calc_zbus(ppc):
    Ybus, Yf, Yt = makeYbus(ppc["baseMVA"], ppc["bus"],  ppc["branch"])
    ppc["internal"]["Yf"] = Yf
    ppc["internal"]["Yt"] = Yt
    ppc["internal"]["Ybus"] = Ybus
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return inv(Ybus)

        
def _extract_results(net, ppc):
    bus_lookup = net._pd2ppc_lookups["bus"]
    net.res_bus_sc = pd.DataFrame(index=net.bus.index)
    net.res_line_sc = pd.DataFrame(index=net.line.index)
    net.res_trafo_sc = pd.DataFrame(index=net.trafo.index)
    net.res_trafo3w_sc = pd.DataFrame(index=net.trafo3w.index)
    ppc_index = bus_lookup[net.bus.index]
    net.res_bus_sc["ikss_ka"] = ppc["bus_sc"][ppc_index, IKSS]
    
    branch_lookup = net._pd2ppc_lookups["branch"]
    if "line" in branch_lookup:
        f, t = branch_lookup["line"]
        net.res_line_sc["ikss_ka"] = ppc["branch_sc"][f:t, IKSS_F].real

    if "trafo" in branch_lookup:
        f, t = branch_lookup["trafo"]
        net.res_trafo_sc["ikss_lv_ka"] = ppc["branch_sc"][f:t, IKSS_F].real
        net.res_trafo_sc["ikss_hv_ka"] = ppc["branch_sc"][f:t, IKSS_T].real
        
    if net._options["ip"]:
        net.res_bus_sc["ip_ka"] = ppc["bus_sc"][ppc_index, IP]

    if net._options["ith"]:
        net.res_bus_sc["ith_ka"] = ppc["bus_sc"][ppc_index, ITH]

if __name__ == '__main__':
    import pandapower as pp
    net = pp.create_empty_network()
    b0 = pp.create_bus(net, 220)
    b1 = pp.create_bus(net, 110)
    b1a = pp.create_bus(net, 110)
    b1b = pp.create_bus(net, 110)
    b2 = pp.create_bus(net, 110)
    b3 = pp.create_bus(net, 110)

    pp.create_switch(net, b1, b1a, et="b")
    pp.create_switch(net, b1, b1b, et="b")
    pp.create_ext_grid(net, b0, s_sc_max_mva=100., s_sc_min_mva=80., rx_min=0.1, rx_max=0.1)
    pp.create_transformer(net, b0, b1, "100 MVA 220/110 kV")
    pp.create_std_type(net, {"c_nf_per_km": 190, "max_i_ka": 0.829, "r_ohm_per_km": 0.0306, \
                            "x_ohm_per_km": 0.1256637}, "PF")
    l1 = pp.create_line(net, b1a, b2, std_type="PF" , length_km=20.)
    l2 = pp.create_line(net, b2, b3, std_type="PF" , length_km=15., in_service=True)
    l3 = pp.create_line(net, b3, b1b, std_type="PF" , length_km=10.)
    pp.create_switch(net, b3, l2, closed=False, et="l")

    net.line["endtemp_degree"] = 250
    runsc(net, network_structure="auto", ip=False, ith=False, r_fault_ohm=0., x_fault_ohm=0.)
    print(net.res_bus_sc)
    print(net.res_line_sc)

