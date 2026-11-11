# Nosotros — Agenda Minimalista para Parejas

Aplicación web y app nativa para Android creada a partir del diseño de Figma Make (**Agenda Minimalista para Parejas**), desplegada en `https://citas.serviciosinformaticos.cloud`.

## Características
- **Agenda compartida**: Vista de próximos planes, tira visual de 7 días, etiquetas por categoría (*Cita*, *Comida*, *Viaje*, *Cultura*, *En casa*, *Otro*) y estado (*Reprogramado*).
- **Gestión sin fricción**: Creación de planes nuevos, reprogramación rápida y marcado como realizado.
- **Álbum de Recuerdos**: Galería estilo masonry con fotografías y notas de cada momento vivido.
- **App Android & PWA**:
  - Proyecto nativo Android (`android/`) con soporte de cámara y galería para subir fotos de recuerdos, *pull-to-refresh* e integración con el botón físico *Atrás*.
  - Compilación automática de `Nosotros-v1.0.0.apk` mediante GitHub Actions (`.github/workflows/build-android.yml`) publicada en **GitHub Releases** y accesible desde `/descargar-apk/`.
  - Soporte completo de PWA (`manifest.webmanifest` + `sw.js`).
