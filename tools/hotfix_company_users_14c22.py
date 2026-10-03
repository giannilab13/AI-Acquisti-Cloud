from pathlib import Path

app_path = Path("static/app.js")
index_path = Path("static/index.html")

app = app_path.read_text(encoding="utf-8")

old_header = """  if(activeCompanyId && !url.startsWith('/api/platform/')){\n    headers.set('X-Company-ID', String(activeCompanyId));\n  }"""
new_header = """  const platformWithoutCompany =\n    url.startsWith('/api/platform/admin/')\n    || url === '/api/platform/companies';\n  if(activeCompanyId && !platformWithoutCompany){\n    headers.set('X-Company-ID', String(activeCompanyId));\n  }"""

if old_header not in app:
    raise SystemExit("Expected getJSON company-header block not found")
app = app.replace(old_header, new_header, 1)

old_refresh = """  if(active.id==='productsView') await loadProductsPage();\n  if(active.id==='suppliersView') await loadSuppliersPage();"""
new_refresh = """  if(active.id==='usersView') await loadCompanyUsers();\n  if(active.id==='productsView') await loadProductsPage();\n  if(active.id==='suppliersView') await loadSuppliersPage();"""

if old_refresh not in app:
    raise SystemExit("Expected refreshCurrentView block not found")
app = app.replace(old_refresh, new_refresh, 1)
app_path.write_text(app, encoding="utf-8")

index = index_path.read_text(encoding="utf-8")
old_version_count = index.count("Cloud 1.4C1")
if old_version_count == 0:
    raise SystemExit("No old Cloud 1.4C1 labels found")
index = index.replace("Cloud 1.4C1", "Cloud 1.4C2.2")
index_path.write_text(index, encoding="utf-8")

print(f"Patched company context and replaced {old_version_count} old version labels.")
