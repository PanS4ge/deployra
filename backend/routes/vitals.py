from fastapi import FastAPI, APIRouter, Response
from utils import vitals, setting, diffiehellman, allowed
import json
import os
import fastapi

router = APIRouter()

@router.get("/back/vitals/usage")
def ram(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_usage_percentage(), "cpu": vitals.get_cpu_usage_percentage()}))

@router.get("/back/vitals/cpuusages")
def ram(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    cpus = vitals.get_cpu_usages_percentage()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({str(core): cpus[core] for core in range(len(cpus))}))

@router.get("/back/vitals/amount")
def maxvitals(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_amount(), "cpu": vitals.get_cpu_core_count(), "rawram": vitals.get_raw_ram_amount()}))
