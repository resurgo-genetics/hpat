from __future__ import print_function, division, absolute_import
import types as pytypes  # avoid confusion with numba.types

import numba
from numba import ir, config
from numba.ir_utils import (mk_unique_var, replace_vars_inner, find_topo_order,
                            dprint_func_ir, remove_dead, mk_alloc)
import hpat
from hpat import hiframes_api
import pandas

class HiFrames(object):
    """analyze and transform hiframes calls"""
    def __init__(self, func_ir):
        self.func_ir = func_ir

        # varname -> 'str'
        self.str_const_table = {}

        # var -> list
        self.map_calls = {}
        self.pd_globals = []
        self.pd_df_calls = []

        # rolling_varname -> column_varname
        self.rolling_vars = {}
        # rolling call name -> [column_varname, win_size]
        self.rolling_calls = {}
        # rolling call agg name -> [column_varname, win_size, func]
        self.rolling_calls_agg = {}

        # df_var -> {col1:col1_var ...}
        self.df_vars = {}
        # df_column -> df_var
        self.df_cols = {}

    def run(self):
        dprint_func_ir(self.func_ir, "starting hiframes")
        topo_order = find_topo_order(self.func_ir.blocks)
        for label in topo_order:
            new_body = []
            for inst in self.func_ir.blocks[label].body:
                if isinstance(inst, ir.Assign):
                    inst_list = self._run_assign(inst)
                    if inst_list is not None:
                        new_body.extend(inst_list)
                else:
                    new_body.append(inst)
            self.func_ir.blocks[label].body = new_body
        remove_dead(self.func_ir.blocks, self.func_ir.arg_names)
        dprint_func_ir(self.func_ir, "after hiframes")
        if config.DEBUG_ARRAY_OPT==1:
            print("df_vars: ", self.df_vars)
        return

    def _run_assign(self, assign):
        lhs = assign.target.name
        rhs = assign.value
        # lhs = pandas
        if (isinstance(rhs, ir.Global) and isinstance(rhs.value, pytypes.ModuleType)
                    and rhs.value==pandas):
            self.pd_globals.append(lhs)

        if isinstance(rhs, ir.Expr):
            # df_call = pd.DataFrame
            if (rhs.op=='getattr' and rhs.value.name in self.pd_globals
                    and rhs.attr=='DataFrame'):
                self.pd_df_calls.append(lhs)

            # df = pd.DataFrame(map_var)
            if rhs.op=='call' and rhs.func.name in self.pd_df_calls:
                # only map input allowed now
                assert len(rhs.args) is 1 and rhs.args[0].name in self.map_calls

                self.df_vars[lhs] = self._process_df_build_map(
                                            self.map_calls[rhs.args[0].name])
                self._update_df_cols()
                # remove DataFrame call
                return []

            # d = df['column']
            if (rhs.op == 'static_getitem' and rhs.value.name in self.df_vars
                                            and isinstance(rhs.index, str)):
                df = rhs.value.name
                assign.value = self.df_vars[df][rhs.index]
                self.df_cols[lhs] = df  # save lhs as column

            # df1 = df[df.A > .5]
            if (rhs.op == 'getitem' and rhs.value.name in self.df_vars):
                # output df1 has same columns as df, create new vars
                scope = assign.target.scope
                loc = assign.target.loc
                self.df_vars[lhs] = {}
                for col, _ in self.df_vars[rhs.value.name].items():
                    self.df_vars[lhs][col] = ir.Var(scope, mk_unique_var(col),
                                                                            loc)
                self._update_df_cols()
                return [hiframes_api.Filter(lhs, rhs.value.name, rhs.index,
                                                        self.df_vars, rhs.loc)]

            # d = df.column
            if rhs.op=='getattr' and rhs.value.name in self.df_vars:
                df = rhs.value.name
                df_cols = self.df_vars[df]
                assert rhs.attr in df_cols
                assign.value = df_cols[rhs.attr]
                self.df_cols[lhs] = df  # save lhs as column

            # d.rolling
            if rhs.op=='getattr' and rhs.value.name in self.df_cols:
                if rhs.attr=='rolling':
                    self.rolling_vars[lhs] = rhs.value.name
                    return []  # remove node

            # d.rolling(3)
            if rhs.op=='call' and rhs.func.name in self.rolling_vars:
                assert len(rhs.args) == 1  # only window size arg
                self.rolling_calls[lhs] = [self.rolling_vars[rhs.func.name], rhs.args[0]]
                return []  # remove

            # d.rolling(3).sum
            if rhs.op=='getattr' and rhs.value.name in self.rolling_calls:
                self.rolling_calls_agg[lhs] = self.rolling_calls[rhs.value.name]
                self.rolling_calls_agg[lhs].append(rhs.attr)
                return []  # remove

            # d.rolling(3).sum()
            if rhs.op=='call' and rhs.func.name in self.rolling_calls_agg:
                print(rhs)

            if rhs.op == 'build_map':
                self.map_calls[lhs] = rhs.items

        # handle copies lhs = f
        if isinstance(rhs, ir.Var) and rhs.name in self.df_vars:
            self.df_vars[lhs] = self.df_vars[rhs.name]
        if isinstance(rhs, ir.Var) and rhs.name in self.df_cols:
            self.df_cols[lhs] = self.df_cols[rhs.name]

        if isinstance(rhs, ir.Const) and isinstance(rhs.value, str):
            self.str_const_table[lhs] = rhs.value
        return [assign]

    def _process_df_build_map(self, items_list):
        df_cols = {}
        for item in items_list:
            col_var = item[0].name
            assert col_var in self.str_const_table
            col_name = self.str_const_table[col_var]
            df_cols[col_name] = item[1]
        return df_cols

    def _update_df_cols(self):
        self.df_cols = {}  # reset
        for df_name, cols_map in self.df_vars.items():
            for col_name, col_var in cols_map.items():
                self.df_cols[col_var.name] = df_name
        return
