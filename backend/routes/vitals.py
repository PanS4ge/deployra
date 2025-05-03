from fastapi import FastAPI, APIRouter, Response
from utils import vitals, setting, diffiehellman
import json
import os
import fastapi

router = APIRouter()

@router.get("/back/vitals/usage")
def ram(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_usage_percentage(), "cpu": vitals.get_cpu_usage_percentage()}))

@router.get("/back/vitals/cpuusages")
def ram(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    cpus = vitals.get_cpu_usages_percentage()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({str(core): cpus[core] for core in range(len(cpus))}))

@router.get("/back/vitals/amount")
def maxvitals(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_amount(), "cpu": vitals.get_cpu_core_count(), "rawram": vitals.get_raw_ram_amount()}))
