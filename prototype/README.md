# Prototipo estático COHIFIS

Este directorio contiene un prototipo visual estático (mobile-first) de COHIFIS como hub comercial multi-producto, con foco en:

- Seguros, créditos y soluciones empresariales.
- Producto destacado: Seguro de Vida Temporal.
- Conversión a WhatsApp/Vicky por ruta de producto/campaña.

## Archivos

- `index.html`: página de Inicio.
- `seguro-de-vida-temporal.html`: página de producto prioritario.
- `styles.css`: estilos compartidos.

## Cómo abrir localmente

No requiere dependencias ni instalación.

1. Abre la carpeta `prototype/`.
2. Haz doble clic en `index.html` para abrir en tu navegador.
3. Navega entre páginas usando los enlaces del header.

Opcional (servidor estático simple con Python ya disponible en muchos entornos):

```bash
cd prototype
python3 -m http.server 8000
```

Luego abre: `http://localhost:8000`

## Notas

- Rutas WhatsApp por producto/campaña:
  - Seguro Auto / SECOM -> Vicky SECOM: `https://wa.me/5216683211342`
  - Vida Temporal, Préstamo IMSS, TPV, créditos empresariales y seguros empresariales -> Vicky Redes/Campañas: `https://wa.me/5216681855146`
- Los enlaces de WhatsApp mantienen mensajes prellenados diferenciados por bloque CTA para trazabilidad comercial.
- El mensaje “hasta 40% de descuento” se mantiene como beneficio condicionado a perfil, cobertura, aseguradora y condiciones de contratación.

## Gobernanza de copy promocional

- Todo claim de descuento, promoción, comisión, beneficio o disponibilidad debe incluir disclaimer cercano y visible.
- No se deben prometer precios, aprobación, descuentos garantizados ni disponibilidad.
