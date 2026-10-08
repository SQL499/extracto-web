"""Pagina local: sube el PDF y descarga el Excel solo si el saldo cuadra."""

import io

import pdfplumber
from openpyxl import Workbook
from openpyxl.styles import Font
from flask import Flask, render_template, request, send_file

from parser import parsear, parsear_bcp_posicionado

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


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


@app.get("/")
def inicio():
    return render_template("index.html")


@app.post("/convertir")
def convertir():
    archivo = request.files.get("pdf")
    if not archivo or not archivo.filename.lower().endswith(".pdf"):
        return render_template("index.html", error="Sube un PDF."), 400
    clave = request.form.get("clave", "")
    try:
        texto, lineas = leer_pdf(archivo.read(), clave)
    except ValueError as exc:
        return render_template("index.html", error=str(exc)), 400
    resultado = parsear(texto, lineas)
    if not resultado.cuadra:
        return render_template("index.html", error=resultado.motivo), 422
    nombre = f"{resultado.banco.lower()}_movimientos.xlsx"
    return send_file(
        a_excel(resultado),
        as_attachment=True,
        download_name=nombre,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
