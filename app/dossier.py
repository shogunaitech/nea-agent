"""Envío del dossier como DOCUMENTO de WhatsApp, no como enlace.

El API de bot del CRM (`POST /api/bot/messages`) solo acepta
`{conversationId, text}`: no hay ruta de bot para adjuntos. El CRM sí sabe
enviar documentos —es lo que hace el clip de la bandeja—, pero su endpoint
(`POST /api/conversations/{id}/messages/media`) exige SESIÓN DE USUARIO
(Better Auth), no la clave de bot.

Aquí se abre esa sesión con una cuenta dedicada de rol `member`, se descarga el
PDF público del dossier y se publica como documento. Todo es best-effort: si
algo falla se devuelve False y el turno sigue con su texto de siempre, así que
el agente nunca se queda mudo por culpa del adjunto.

El token de WhatsApp sigue sin salir del CRM: Nea nunca habla con Meta.
"""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger("nea.dossier")

_SIGN_IN = "/api/auth/sign-in/email"
_MEDIA = "/api/conversations/{conv}/messages/media"
_TIMEOUT = 30.0
_NOMBRE_FICHERO = "SHOGUN_AI_Dossier_2026.pdf"

# Se reconoce el nombre del fichero, no la URL exacta: si algún día cambia el
# dominio, el envío del documento sigue funcionando igual.
_ENLACE = re.compile(r"https?://\S*dossier\.pdf", re.IGNORECASE)

# Pie de foto de reserva, solo si al quitar el enlace el texto queda vacío.
_PIE_POR_DEFECTO = "Aquí tienes el dossier de servicios de Shogun AI."


def pie_de_dossier(texto: str) -> str | None:
    """Si la respuesta lleva el enlace del dossier, devuelve el pie de foto.

    Quita el enlace y la puntuación que queda colgando, para que el documento no
    viaje con un «aquí lo tienes:» huérfano. Si no hay enlace, devuelve None.
    """
    if not _ENLACE.search(texto):
        return None
    limpio = _ENLACE.sub("", texto)
    limpio = re.sub(r"[ \t]*([:;,])[ \t]*(?=\n|$)", r"\1", limpio)
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio).strip()
    return limpio or _PIE_POR_DEFECTO


class DossierSender:
    """Publica el PDF del dossier en una conversación, con sesión de usuario."""

    def __init__(
        self, base_url: str, email: str, password: str, dossier_url: str
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._email = email or ""
        self._password = password or ""
        self._url = dossier_url or ""
        self._cookie = ""

    @property
    def activo(self) -> bool:
        return bool(self._base and self._email and self._password and self._url)

    async def _sesion(self, http: httpx.AsyncClient) -> str:
        """Cookie de la cuenta dedicada, reutilizada mientras el CRM la acepte."""
        if self._cookie:
            return self._cookie
        resp = await http.post(
            f"{self._base}{_SIGN_IN}",
            json={"email": self._email, "password": self._password},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"login del CRM devolvió {resp.status_code}")
        galletas = resp.headers.get_list("set-cookie")
        if not galletas:
            raise RuntimeError("el CRM no devolvió sesión")
        self._cookie = "; ".join(c.split(";", 1)[0] for c in galletas)
        return self._cookie

    async def enviar(self, conversation_id: str, pie: str) -> bool:
        """True si el documento salió. Nunca lanza: el adjunto no tumba el turno."""
        if not self.activo:
            return False
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True
            ) as http:
                pdf = await http.get(self._url)
                if pdf.status_code != 200 or not pdf.content:
                    logger.warning("dossier: el PDF devolvió %s", pdf.status_code)
                    return False
                cookie = await self._sesion(http)
                resp = await http.post(
                    f"{self._base}{_MEDIA.format(conv=conversation_id)}",
                    headers={"cookie": cookie},
                    files={"file": (_NOMBRE_FICHERO, pdf.content, "application/pdf")},
                    data={"caption": pie[:1024]},
                )
                if resp.status_code in (200, 201):
                    logger.info(
                        "dossier enviado como documento (conv %s, %d KB)",
                        conversation_id,
                        len(pdf.content) // 1024,
                    )
                    return True
                if resp.status_code == 401:
                    # Sesión caducada: se tira para que el próximo intento vuelva a entrar.
                    self._cookie = ""
                logger.warning(
                    "dossier: el CRM rechazó el documento (%s) %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception:
            logger.exception("dossier: fallo enviando el documento")
            return False
