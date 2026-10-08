"""Parsea estados de cuenta de Interbank y BBVA y no entrega filas si el saldo no cuadra."""

import re
from dataclasses import dataclass


@dataclass
class Movimiento:
    fecha: str
    concepto: str
    ingreso: float
    gasto: float
    saldo: float


@dataclass
class Resultado:
    banco: str
    saldo_inicial: float
    saldo_final: float
    movimientos: list
    cuadra: bool
    motivo: str


def _num(raw: str) -> float:
    texto = raw.strip().replace(",", "")
    signo = -1.0 if texto.endswith("-") else 1.0
    if texto.endswith("-"):
        texto = texto[:-1]
    if texto.startswith("+"):
        texto = texto[1:]
    if texto.startswith("-"):
        signo = -1.0
        texto = texto[1:]
    return signo * float(texto)


def _fila(fecha: str, concepto: str, monto: float, saldo: float) -> Movimiento:
    return Movimiento(
        fecha=fecha,
        concepto=concepto.strip(),
        ingreso=monto if monto > 0 else 0.0,
        gasto=-monto if monto < 0 else 0.0,
        saldo=saldo,
    )


def _cuadra(inicio: float, filas: list) -> tuple:
    saldo = inicio
    for fila in filas:
        saldo = round(saldo + fila.ingreso - fila.gasto, 2)
        if abs(saldo - fila.saldo) > 0.001:
            return False, saldo
    return True, saldo


def parsear_interbank(texto: str) -> Resultado:
    inicio = None
    filas = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if "SALDO CONTABLE AL" in linea:
            break
        if "EMPEZASTE" in linea:
            hallado = re.search(r"([\d,]+\.\d{2})\s*$", linea)
            if hallado:
                inicio = _num(hallado.group(1))
            continue
        hallado = re.match(
            r"(\d{2}/\d{2}/\d{4})\s+(.+?)\s+([+-]?[\d,]+\.\d{2})\s+([\d,]+\.\d{2})$",
            linea,
        )
        if not hallado:
            continue
        fecha, concepto, monto, saldo = hallado.groups()
        filas.append(_fila(fecha, concepto, _num(monto), _num(saldo)))
    if inicio is None or not filas:
        return Resultado("Interbank", 0, 0, [], False, "No se reconocio el formato de Interbank.")
    ok, calculado = _cuadra(inicio, filas)
    return Resultado(
        "Interbank",
        inicio,
        filas[-1].saldo,
        filas,
        ok,
        "Saldo cerrado." if ok else f"El saldo no cuadra: calculado {calculado:.2f}.",
    )


def parsear_bbva(texto: str) -> Resultado:
    inicio = None
    filas = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if "SALDO ANTERIOR" in linea:
            hallado = re.search(r"([\d,]+\.\d{2})\s*$", linea)
            if hallado:
                inicio = _num(hallado.group(1))
            continue
        hallado = re.match(
            r"(\d{2}-\d{2})\s+(\d{2}-\d{2})\s+(.+?)\s+([\d,]+\.\d{2}-?)\s+([\d,]+\.\d{2})$",
            linea,
        )
        if not hallado:
            continue
        fecha, _valor, resto, monto, saldo = hallado.groups()
        concepto = re.split(r"\s+(MEDIO DE PAGO|BANCA MOVIL|REC Y DOMICIL)", resto)[0]
        filas.append(_fila(fecha, concepto, _num(monto), _num(saldo)))
    if inicio is None or not filas:
        return Resultado("BBVA", 0, 0, [], False, "No se reconocio el formato de BBVA.")
    ok, calculado = _cuadra(inicio, filas)
    return Resultado(
        "BBVA",
        inicio,
        filas[-1].saldo,
        filas,
        ok,
        "Saldo cerrado." if ok else f"El saldo no cuadra: calculado {calculado:.2f}.",
    )


MESES = {
    "ENE": "01", "FEB": "02", "MAR": "03", "ABR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AGO": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DIC": "12",
}


def _anio(texto: str) -> str:
    hallado = re.search(r"DEL\s+\d{2}/\d{2}/(\d{2})", texto)
    return "20" + hallado.group(1) if hallado else ""


def parsear_bcp_posicionado(lineas: list, texto: str) -> Resultado:
    cargo_x = abono_x = None
    for linea in lineas:
        for palabra in linea:
            if palabra["text"].startswith("CARGO") or palabra["text"] == "DEBE":
                cargo_x = palabra["x0"]
            if palabra["text"].startswith("ABONO") or palabra["text"] == "HABER":
                abono_x = palabra["x0"]
    if cargo_x is None or abono_x is None:
        return Resultado("BCP", 0, 0, [], False, "No encontre las columnas del BCP.")
    corte = (cargo_x + abono_x) / 2
    anio = _anio(texto)
    inicio = None
    final = None
    total_cargo = total_abono = None
    filas = []
    for linea in lineas:
        palabras = [p for p in linea if p["text"] not in {"1", "DE"}]
        unidos = " ".join(p["text"] for p in palabras)
        montos = [p for p in palabras if re.fullmatch(r"[\d,]+\.\d{2}", p["text"])]
        if "SALDO ANTERIOR" in unidos and montos:
            inicio = _num(montos[-1]["text"])
            continue
        if unidos.startswith("SALDO") and "ANTERIOR" not in unidos and montos:
            final = _num(montos[-1]["text"])
            continue
        if "TOTAL MOVIMIENTO" in unidos and len(montos) >= 2:
            total_cargo, total_abono = _num(montos[0]["text"]), _num(montos[1]["text"])
            continue
        fechas = [p for p in palabras if re.fullmatch(r"\d{2}[A-Z]{3}", p["text"])]
        if not fechas or not montos or "SALDO" in unidos or "TOTAL" in unidos:
            continue
        fecha = fechas[0]["text"]
        mes = MESES.get(fecha[2:], "00")
        concepto = " ".join(
            p["text"] for p in palabras
            if p not in fechas and p not in montos
        )
        monto = _num(montos[0]["text"])
        if montos[0]["x0"] >= corte:
            filas.append(_fila(f"{fecha[:2]}/{mes}/{anio}", concepto, monto, 0))
        else:
            filas.append(_fila(f"{fecha[:2]}/{mes}/{anio}", concepto, -monto, 0))
    if inicio is None or not filas:
        return Resultado("BCP", 0, 0, [], False, "La clave abrio el PDF, pero no leí los movimientos del BCP.")
    saldo = inicio
    for fila in filas:
        saldo = round(saldo + fila.ingreso - fila.gasto, 2)
        fila.saldo = saldo
    cargos = round(sum(f.gasto for f in filas), 2)
    abonos = round(sum(f.ingreso for f in filas), 2)
    if final is not None and abs(saldo - final) > 0.001:
        return Resultado("BCP", inicio, saldo, filas, False, f"El saldo no cuadra: calculado {saldo:.2f}.")
    if total_cargo is not None and (abs(cargos - total_cargo) > 0.001 or abs(abonos - total_abono) > 0.001):
        return Resultado("BCP", inicio, saldo, filas, False, "Los totales del BCP no cierran.")
    return Resultado("BCP", inicio, filas[-1].saldo, filas, True, "Saldo cerrado.")


def vista_previa(texto: str) -> str:
    lineas = []
    for linea in texto.splitlines():
        limpia = re.sub(r"\d{6,}", "####", linea.strip())
        if limpia:
            lineas.append(limpia[:90])
        if len(lineas) == 8:
            break
    if not lineas:
        return "El PDF abrio, pero no salio texto."
    return "El PDF abrio, pero el formato no coincide. Primeras lineas: " + " | ".join(lineas)


def parsear(texto: str, lineas=None) -> Resultado:
    if "EMPEZASTE" in texto and "Saldo Contable" in texto:
        return parsear_interbank(texto)
    if "SALDO ANTERIOR" in texto and "CARGO/ABONO" in texto:
        return parsear_bbva(texto)
    if lineas and "ABONOS" in texto and "CARGOS" in texto:
        return parsear_bcp_posicionado(lineas, texto)
    if re.search(r"^\d{2}-\d{2}\s+", texto, re.M):
        return Resultado("BCP", 0, 0, [], False, "Este corte del BCP no trae columnas de cargo y abono.")
    return Resultado("", 0, 0, [], False, vista_previa(texto))
