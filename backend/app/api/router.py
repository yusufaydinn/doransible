"""Uygulamanın kök router'ı."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import controller_paths, executions, health, inventories, jobs, projects

root_router = APIRouter()
root_router.include_router(health.router)

# Domain endpoint'leri MIMARI.md bölüm 7'deki taslağa uygun olarak bu prefix
# altındadır. AI router'ı EPIC 3 ve sonrasında eklenecektir.
api_router = APIRouter(prefix="/api")
api_router.include_router(projects.router)
api_router.include_router(inventories.router)
# Controller path browse (R1-V3J0C): Project/Inventory formlarının "Gözat…"
# dialogu için tek, salt-okunur listeleme yüzeyi. Ayrı bir router'dır çünkü
# project/inventory kaydı üretmez, yalnızca onların allowlist'lerini okur.
api_router.include_router(controller_paths.router)
# Execution planı da project altındadır ama ayrı bir router'dır: plan üretimi
# project CRUD'undan farklı bir domain'dir ve ileride gerçek execution
# endpoint'leri bu router'a eklenecektir (R1-V1).
api_router.include_router(executions.router)
# Job okuma yüzeyi (liste/detay/sonuç) ayrı bir router'dır: execution.router
# yalnız Job'ı kuyruğa alır, buradaki route'lar yalnız zaten var olan bir
# Job'ı okur (R1-V3D2B).
api_router.include_router(jobs.router)
