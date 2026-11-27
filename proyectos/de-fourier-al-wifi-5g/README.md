# De Fourier al WiFi/5G

Proyecto final del curso de Señales y Sistemas. Explica, paso a paso, los principios de
procesamiento de señales que hacen posible una transmisión digital inalámbrica moderna
(WiFi y 5G), mediante simulaciones, visualizaciones interactivas y un video de divulgación.

**Video:** [youtube.com/watch?v=Iqr9_2SShrs](https://www.youtube.com/watch?v=Iqr9_2SShrs)

---

## Objetivos

- Analizar el contenido espectral de señales con la Transformada Discreta de Fourier (DFT/FFT).
- Diseñar e implementar filtros digitales pasa-bajos y pasa-banda.
- Generar señales analíticas con la Transformada de Hilbert y construir señales I/Q.
- Simular la modulación y demodulación QAM y visualizar su diagrama de constelación.
- Integrar todo en un dashboard interactivo con Streamlit.
- Divulgar los resultados en un video integrado en el propio dashboard.

## Cadena de procesamiento

```text
Señal ──► FFT (análisis espectral) ──► Filtrado ──► Hilbert → I/Q ──► Modulación QAM ──► Constelación
```

## Contenido

| Archivo | Descripción |
|---|---|
| [`dashboard-streamlit.ipynb`](dashboard-streamlit.ipynb) | Código del dashboard (genera las páginas de Streamlit). |
| [`docs/diagrama-de-flujo.pdf`](docs/diagrama-de-flujo.pdf) | Diagrama de flujo de la aplicación. |
| [`docs/enunciado.pdf`](docs/enunciado.pdf) | Enunciado del proyecto (material del curso). |

## Ejecución

El cuaderno está pensado para Google Colab: instala las dependencias, escribe las páginas de
la aplicación y la publica. Localmente:

```bash
pip install streamlit numpy scipy matplotlib
jupyter notebook dashboard-streamlit.ipynb
```

## Autores

- Miguel Andrés Peña Fandiño
- Sofía López Usma
- Juan Fernando Montoya Ortiz
