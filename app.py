"""Sube el PDF y descarga el Excel solo si el saldo cuadra. Una vez gratis."""

import io
import json
import os
import urllib.request
from urllib.error import HTTPError

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Font
from flask import Flask, make_response, redirect, render_template, request, send_file

from parser import parsear

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
PRECIO = 15
USADOS = set()


def preparar(blob: bytes) -> bytes:
    inicio = blob.find(b"%PDF")
    return blob[inicio:] if inicio != -1 else blob


def leer_pdf(blob: bytes, clave: str):
    try:
        with pdfplumber.open(io.BytesIO(preparar(blob)), password=clave or None) as pdf:
            textos = []
            lineas = []
            for pagina in pdf.pages[:12]:
                palabras = pagina.extract_words() or []
                grupos = {}
                for palabra in palabras:
                    grupos.setdefault(round(palabra["top"]), []).append(palabra)
                for top in sorted(grupos):
                    fila = sorted(grupos[top], key=lambda p: p["x0"])
                    lineas.append([{"text": p["text"], "x0": p["x0"]} for p in fila])
                textos.append(pagina.extract_text() or "")
        texto = "\n".join(textos)
        if not texto.strip():
            texto = "\n".join(" ".join(p["text"] for p in fila) for fila in lineas)
        return texto, lineas
    except Exception as exc:
        if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise ValueError("La contrasena no abre este PDF.") from exc
        raise


def a_excel(resultado) -> io.BytesIO:
    libro = Workbook()
    hoja = libro.active
    hoja.title = "Movimientos"
    hoja.append(["Fecha", "Concepto", "Ingreso", "Gasto", "Saldo"])
    for celda in hoja[1]:
        celda.font = Font(bold=True)
    for fila in resultado.movimientos:
        hoja.append([fila.fecha, fila.concepto, fila.ingreso, fila.gasto, fila.saldo])
    hoja.append([])
    hoja.append(["Saldo inicial", resultado.saldo_inicial])
    hoja.append(["Saldo final", resultado.saldo_final])
    for columna, ancho in zip("ABCDE", (14, 42, 14, 14, 14)):
        hoja.column_dimensions[columna].width = ancho
    salida = io.BytesIO()
    libro.save(salida)
    salida.seek(0)
    return salida


def base_url() -> str:
    return os.environ.get("APP_URL", request.url_root).rstrip("/")


def mp(ruta: str, cuerpo=None):
    token = os.environ.get("MP_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("Falta MP_ACCESS_TOKEN en el servidor.")
    datos = None if cuerpo is None else json.dumps(cuerpo).encode()
    pedido = urllib.request.Request(
        "https://api.mercadopago.com" + ruta,
        data=datos,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(pedido, timeout=20) as respuesta:
            return json.loads(respuesta.read().decode())
    except HTTPError as exc:
        raise ValueError("Mercado Pago rechazo el cobro.") from exc


def puede_convertir() -> str:
    if not request.cookies.get("uso_gratis"):
        return "gratis"
    pago = request.cookies.get("credito")
    return "pagado" if pago and pago not in USADOS else ""


def marcar(respuesta, modo: str):
    if modo == "gratis":
        respuesta.set_cookie("uso_gratis", "1", max_age=60 * 60 * 24 * 30, samesite="Lax")
    if modo == "pagado":
        USADOS.add(request.cookies.get("credito"))
        respuesta.delete_cookie("credito")
    return respuesta


@app.get("/")
def inicio():
    aviso = request.args.get("aviso")
    return render_template("index.html", aviso=aviso, bloqueado=puede_convertir() == "")


@app.get("/pagar")
def pagar():
    preferencia = mp(
        "/checkout/preferences",
        {
            "items": [{
                "title": "Conversion extra de estado de cuenta",
                "quantity": 1,
                "currency_id": "PEN",
                "unit_price": PRECIO,
            }],
            "back_urls": {
                "success": base_url() + "/pago/ok",
                "failure": base_url() + "/pago/fallo",
                "pending": base_url() + "/pago/fallo",
            },
            "auto_return": "approved",
        },
    )
    return redirect(preferencia["init_point"])


@app.get("/pago/ok")
def pago_ok():
    pago_id = request.args.get("payment_id") or request.args.get("collection_id")
    if not pago_id:
        return redirect("/?aviso=no-pago")
    pago = mp(f"/v1/payments/{pago_id}")
    if pago.get("status") != "approved" or float(pago.get("transaction_amount", 0)) != PRECIO:
        return redirect("/?aviso=no-pago")
    respuesta = redirect("/?aviso=pagado")
    respuesta.set_cookie("credito", pago_id, max_age=60 * 60 * 24, samesite="Lax")
    return respuesta


@app.get("/pago/fallo")
def pago_fallo():
    return redirect("/?aviso=no-pago")


@app.post("/convertir")
def convertir():
    modo = puede_convertir()
    if not modo:
        return render_template("index.html", bloqueado=True), 402
    archivo = request.files.get("pdf")
    if not archivo or not archivo.filename.lower().endswith(".pdf"):
        return render_template("index.html", error="Sube un PDF."), 400
    try:
        texto, lineas = leer_pdf(archivo.read(), request.form.get("clave", ""))
    except ValueError as exc:
        return render_template("index.html", error=str(exc)), 400
    resultado = parsear(texto, lineas)
    if not resultado.cuadra:
        return render_template("index.html", error=resultado.motivo), 422
    respuesta = make_response(send_file(
        a_excel(resultado),
        as_attachment=True,
        download_name=f"{resultado.banco.lower()}_movimientos.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ))
    return marcar(respuesta, modo)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
