import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, r"g:\Progetti\Visual Studio Code\LORE - Copia")

import pytest
from parsers.go_taint_tracer import GoASTTaintTracer
from parsers.ts_taint_tracer import TSASTTaintTracer
from cli.gh_check import _get_function_param_name

def test_go_cross_file_taint_tracing(tmp_path):
    file1_code = """package main
import "net/http"

def handleRequest(w http.ResponseWriter, r *http.Request) {
    userInput := r.FormValue("query")
    doProcess(userInput)
}
"""
    tracer1 = GoASTTaintTracer(file1_code)
    res1 = tracer1.trace()
    
    assert len(res1["outgoing_calls"]) >= 1
    call = res1["outgoing_calls"][0]
    assert call["func_name"] == "doProcess"
    assert call["var_name"] == "userInput"

    file2_path = tmp_path / "helper.go"
    file2_code = """package main

func doProcess(rawQuery string) {
    db.Query(rawQuery)
}
"""
    file2_path.write_text(file2_code, encoding="utf-8")
    
    param_name = _get_function_param_name(file2_path, "doProcess", arg_index=0, arg_name=None)
    assert param_name == "rawQuery"
    
    tracer2 = GoASTTaintTracer(file2_code, external_sources={param_name})
    res2 = tracer2.trace()
    assert len(res2["flows"]) == 1
    assert res2["flows"][0]["sink_name"] == "db.Query"

def test_ts_cross_file_taint_tracing(tmp_path):
    file1_code = """import { doProcess } from './helper';

function onRequest(req: any) {
    const inputData = req.query.cmd;
    doProcess(inputData);
}
"""
    tracer1 = TSASTTaintTracer(file1_code)
    res1 = tracer1.trace()
    
    assert len(res1["outgoing_calls"]) >= 1
    call = res1["outgoing_calls"][0]
    assert call["func_name"] == "doProcess"
    assert call["var_name"] == "inputData"

    file2_path = tmp_path / "helper.ts"
    file2_code = """import { exec } from 'child_process';

export function doProcess(cmdString: string) {
    exec(cmdString);
}
"""
    file2_path.write_text(file2_code, encoding="utf-8")
    
    param_name = _get_function_param_name(file2_path, "doProcess", arg_index=0, arg_name=None)
    assert param_name == "cmdString"
    
    tracer2 = TSASTTaintTracer(file2_code, external_sources={param_name})
    res2 = tracer2.trace()
    assert len(res2["flows"]) == 1
    assert res2["flows"][0]["sink_name"] == "exec"
