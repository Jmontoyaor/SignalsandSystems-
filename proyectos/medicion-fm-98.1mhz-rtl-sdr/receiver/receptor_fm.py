"""Receptor WBFM con RTL-SDR: sintonia 90-100 MHz, calibracion de frecuencia,
audio continuo y espectros (RF, canal y multiplex FM patron).

Uso:
    python3 receptor_fm.py                 # ventana grafica, arranca en 98,0 MHz
    python3 receptor_fm.py -f 105.7        # otra frecuencia inicial
    python3 receptor_fm.py --prueba 8 -f 105.7 --wav prueba.wav   # sin ventana ni parlantes

Cadena: rtl_tcp (2,4 MS/s) -> desplazamiento digital -> FIR + /10 (240 kS/s)
-> discriminador FM (multiplex) -> FIR 15 kHz + /5 (48 kS/s) -> de-enfasis 75 us -> aplay.
"""
import argparse, json, os, queue, socket, struct, subprocess, sys, threading, time, wave
import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(AQUI, 'calibracion.json')

FS = 2_400_000                 # muestreo del RTL-SDR
D1, D2 = 10, 5                 # 2,4 MS/s -> 240 kS/s -> 48 kS/s
FS1, FS2 = FS // D1, FS // D1 // D2
BLOQUE = 120_000               # 50 ms; multiplo de D1*D2 para no romper la fase de diezmado
OFFSET = 250_000               # el LO se pone 250 kHz abajo: la estacion queda lejos del pico de DC
F_MIN, F_MAX = 90.0e6, 100.0e6  # rango de sintonia: 90 a 100 MHz
TAU = 75e-6                    # de-enfasis de America
DEV = 75_000                   # desviacion pico de la radio comercial


def fir(taps, corte, fs):
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * corte / fs * n) * np.blackman(taps)
    return h / h.sum()


class FiltroDiezmador:
    """FIR por solapamiento (overlap-save) que conserva el estado entre bloques.
    Reiniciar el filtro en cada bloque es lo que produce chasquidos y audio entrecortado."""

    def __init__(self, h, d, n_bloque, complejo):
        self.d, self.m = d, len(h) - 1
        self.nfft = 1 << int(np.ceil(np.log2(n_bloque + self.m)))
        self.h = h
        self.H = np.fft.fft(h, self.nfft)
        self.cola = np.zeros(self.m, complex if complejo else float)
        self.fase = 0          # posicion del proximo diezmado respecto al bloque
        self.complejo = complejo

    def cambiar(self, h):
        assert len(h) - 1 == self.m
        self.h, self.H = h, np.fft.fft(h, self.nfft)

    def __call__(self, x):
        xe = np.concatenate([self.cola, x])
        self.cola = xe[-self.m:]
        y = np.fft.ifft(np.fft.fft(xe, self.nfft) * self.H)[self.m:self.m + len(x)]
        if not self.complejo:
            y = y.real
        out = y[self.fase::self.d]
        self.fase = (self.fase - len(x)) % self.d
        return out


class Receptor:
    def __init__(self, f0, ganancia, audio=True, wav=None, puerto=1234, libre=False):
        self.libre = libre       # solo para calibrar con una estacion fuerte fuera de 90-100 MHz
        cal = {}
        if os.path.exists(CALIB):
            try:
                cal = json.load(open(CALIB))
            except Exception:
                cal = {}
        self.ppm = float(cal.get('ppm', 0.0))
        self.f = float(f0 if libre else np.clip(f0, F_MIN, F_MAX))
        self.ganancia = ganancia
        self.aviso = ''
        self.volumen = 0.8
        self.puerto = puerto
        self.audio, self.wav = audio, wav
        self.vivo = True

        self.bw_canal = 100e3     # media banda del filtro de canal: Carson de la radio comercial ~ 2*(75+15) kHz
        self.f1 = FiltroDiezmador(fir(201, self.bw_canal, FS), D1, BLOQUE, True)
        self.f2 = FiltroDiezmador(fir(201, 15.5e3, FS1), D2, BLOQUE // D1, False)
        self.fase_nco = 0.0
        self.z_prev = 1 + 0j
        self.a_de = np.exp(-1 / (FS2 * TAU))
        self.y_de = 0.0

        self.q_iq = queue.Queue()
        self.q_audio = queue.Queue()
        self.lock = threading.Lock()
        self.esp_ancho = self.esp_canal = self.esp_mpx = None
        self.stats = dict(bloques=0, descartados=0, cortes=0, ajustes_reloj=0, carga=0.0,
                          error_hz=0.0, piloto_db=0.0, snr_db=0.0, perdidas_usb=0)
        self._acum_err = []
        self._calibrando = None
        self._silencio = 0
        self.medidas = None

    # ---------- rtl_tcp ----------
    def arrancar(self):
        self.proc = subprocess.Popen(['rtl_tcp', '-a', '127.0.0.1', '-p', str(self.puerto), '-s', str(FS),
                                      '-f', str(int(self.f - OFFSET)), '-g', str(self.ganancia), '-b', '30'],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._log_rtl, daemon=True).start()
        for _ in range(60):
            try:
                self.sock = socket.create_connection(('127.0.0.1', self.puerto), timeout=2)
                break
            except OSError:
                time.sleep(0.1)
                if self.proc.poll() is not None:
                    raise RuntimeError('rtl_tcp no arranco (¿el RTL-SDR esta conectado o en uso?)')
        else:
            raise RuntimeError('no se pudo conectar con rtl_tcp')
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.settimeout(3)
        cab = b''
        while len(cab) < 12:
            cab += self.sock.recv(12 - len(cab))
        self._cmd(0x02, FS)
        self.poner_ganancia(self.ganancia)
        self.sintonizar(self.f)
        threading.Thread(target=self._lector, daemon=True).start()
        threading.Thread(target=self._dsp, daemon=True).start()
        if self.audio:
            threading.Thread(target=self._salida_audio, daemon=True).start()

    def _log_rtl(self):
        for linea in self.proc.stdout:
            if linea.startswith('ll+'):      # rtl_tcp acumula buffers: el cliente no lee a tiempo
                self.stats['perdidas_usb'] += 1

    def _cmd(self, c, v):
        self.sock.sendall(struct.pack('>BI', c, int(v) & 0xFFFFFFFF))

    def sintonizar(self, f):
        self.f = float(f if self.libre else np.clip(f, F_MIN, F_MAX))
        self._cmd(0x01, int(round(self.f - OFFSET)))
        self._silencio = 4           # 200 ms mientras el PLL se engancha
        self._acum_err = []

    def poner_filtro(self, media_banda):
        """Estrechar el canal deja fuera ruido; por debajo de ~75 kHz empieza a recortar la propia FM
        (con 75 kHz de desviacion la emisora ocupa ~±100 kHz)."""
        self.bw_canal = float(media_banda)
        self.f1.cambiar(fir(201, self.bw_canal, FS))

    def poner_ganancia(self, g):
        self.ganancia = g
        self._cmd(0x03, 1)
        self._cmd(0x04, int(round(g * 10)))

    def calibrar(self, segundos=3.0):
        """Mide el corrimiento de la portadora de la estacion sintonizada y lo convierte en ppm."""
        if self.stats['snr_db'] < 10:
            # con poca señal el ruido y los canales vecinos sesgan la medida (se vieron errores de cientos de ppm)
            self.aviso = f'Señal de {self.stats["snr_db"]:.0f} dB: muy debil para calibrar (minimo 10 dB)'
            return False
        self.aviso = ''
        self._calibrando = []
        self._cal_n = int(segundos * FS / BLOQUE)
        return True

    def cerrar(self):
        self.vivo = False
        try:
            self.sock.close()
        except Exception:
            pass
        if getattr(self, 'proc', None):
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, 'aplay', None):
            self.aplay.terminate()

    # ---------- hilos ----------
    def _lector(self):
        """Solo lee del socket: nunca debe frenarse, o el RTL-SDR pierde muestras."""
        buf = bytearray()
        n = 2 * BLOQUE
        while self.vivo:
            try:
                d = self.sock.recv(1 << 18)
            except (socket.timeout, OSError):
                if self.vivo:
                    continue
                return
            if not d:
                return
            buf += d
            while len(buf) >= n:
                self.q_iq.put(bytes(buf[:n]))
                del buf[:n]

    def _dsp(self):
        while self.vivo:
            try:
                raw = self.q_iq.get(timeout=1)
            except queue.Empty:
                continue
            while self.q_iq.qsize() > 20:         # mas de 1 s de atraso: se descarta para no acumular latencia
                self.q_iq.get()
                self.stats['descartados'] += 1
            t0 = time.perf_counter()
            self._procesar(np.frombuffer(raw, np.uint8))
            dt = time.perf_counter() - t0
            self.stats['carga'] = 0.9 * self.stats['carga'] + 0.1 * dt / (BLOQUE / FS)

    def _procesar(self, u):
        x = (u[0::2].astype(np.float32) - 127.4) + 1j * (u[1::2].astype(np.float32) - 127.4)
        # error del oscilador: la estacion cae en OFFSET - f*e; se corrige en digital y queda en 0 Hz
        desp = OFFSET - self.f * self.ppm * 1e-6
        w = -2 * np.pi * desp / FS
        nco = np.exp(1j * (self.fase_nco + w * np.arange(BLOQUE)))
        self.fase_nco = (self.fase_nco + w * BLOQUE) % (2 * np.pi)
        b = self.f1(x * nco)                                   # 240 kS/s complejo
        z = np.concatenate([[self.z_prev], b])
        self.z_prev = b[-1]
        mpx = np.angle(z[1:] * np.conj(z[:-1])) * FS1 / (2 * np.pi)   # frecuencia instantanea en Hz
        a = self.f2(mpx)                                       # 48 kS/s
        y = np.empty_like(a)                                   # de-enfasis: y = a*y + (1-a)*x, con estado
        k, s = self.a_de, self.y_de
        for i, v in enumerate(a):
            s = k * s + (1 - k) * v
            y[i] = s
        self.y_de = s

        err = float(mpx.mean())
        self._acum_err = (self._acum_err + [err])[-20:]
        self.stats['error_hz'] = float(np.mean(self._acum_err))
        if self._calibrando is not None and self._silencio == 0:
            self._calibrando.append(err)
            if len(self._calibrando) >= self._cal_n:
                delta = float(np.median(self._calibrando))
                self.ppm -= delta / self.f * 1e6                # e_real = e_ant - delta / f
                self._calibrando = None
                self._acum_err = []
                json.dump({'ppm': round(self.ppm, 3), 'f_ref_mhz': self.f / 1e6, 'delta_hz': round(delta, 1),
                           'fecha': time.strftime('%Y-%m-%d %H:%M')}, open(CALIB, 'w'), indent=1)

        pcm = np.clip(y / DEV * self.volumen * 32767, -32767, 32767).astype('<i2')
        if self._silencio:
            self._silencio -= 1
            pcm[:] = 0
        if self.audio:
            self.q_audio.put(pcm.tobytes())
        if self.wav is not None:
            self.wav.writeframes(pcm.tobytes())

        self.stats['bloques'] += 1
        if self.stats['bloques'] % 2 == 0:
            self._espectros(x, b, mpx)

    def _espectros(self, x, b, mpx):
        def psd(s, n, fs):
            win = np.hanning(n)
            s = s[:len(s) // n * n].reshape(-1, n) * win
            return np.mean(np.abs(np.fft.fft(s, axis=1)) ** 2, axis=0) / (fs * np.sum(win ** 2))
        xn = x[:65536] / 127.5                                   # escala del ADC: fondo de escala = 1
        ancho = 10 * np.log10(np.fft.fftshift(psd(xn, 2048, FS)) + 1e-20)
        canal = 10 * np.log10(np.fft.fftshift(psd(b / 127.5, 1024, FS1)) + 1e-20)
        m = 10 * np.log10(2 * psd(mpx / DEV, 2048, FS1)[:1024] + 1e-20)   # unilateral
        prom = lambda viejo, nuevo: nuevo if viejo is None or viejo.shape != nuevo.shape else 0.75 * viejo + 0.25 * nuevo
        with self.lock:
            self.esp_ancho = prom(self.esp_ancho, ancho)
            self.esp_canal = prom(self.esp_canal, canal)
            self.esp_mpx = prom(self.esp_mpx, m)
            fm = np.arange(1024) * FS1 / 2048
            pil = self.esp_mpx[np.abs(fm - 19e3) < 150].max()
            veci = np.median(self.esp_mpx[(np.abs(fm - 19e3) > 600) & (np.abs(fm - 19e3) < 2500)])
            self.stats['piloto_db'] = float(pil - veci)
            fa = (np.arange(2048) - 1024) * FS / 2048 - OFFSET     # 0 = estacion sintonizada
            dentro = self.esp_ancho[np.abs(fa) < min(80e3, self.bw_canal)].mean()
            fuera = np.median(self.esp_ancho)
            self.stats['snr_db'] = float(dentro - fuera)
            self._medidas(fuera)

    def _medidas(self, piso_db):
        """Frecuencia central, ancho de banda ocupado (98 %) y potencia sobre la PSD del canal,
        restando antes el piso de ruido (mismo algoritmo P % del informe)."""
        fk = (np.arange(1024) - 512) * FS1 / 1024
        df = FS1 / 1024
        banda = np.abs(fk) <= self.bw_canal
        p = 10 ** (self.esp_canal[banda] / 10)
        piso = 10 ** (piso_db / 10)
        util = np.clip(p - piso, 0, None)
        total = util.sum()
        if total <= 0:
            self.medidas = None
            return
        c = np.cumsum(util) / total
        f = fk[banda]
        f1, f2 = f[np.searchsorted(c, 0.01)], f[min(np.searchsorted(c, 0.99), len(f) - 1)]
        centro = float((util * f).sum() / total)
        self.medidas = dict(fc=self.f + centro, desvio=centro, bw=float(f2 - f1), f1=float(f1), f2=float(f2),
                            pot_dbfs=float(10 * np.log10(p.sum() * df)),
                            pot_ruido=float(10 * np.log10(total / (piso * banda.sum()))))

    def _salida_audio(self):
        """aplay con 0,5 s de buffer y 0,3 s de precarga: absorbe el ritmo irregular de Python.
        La cola se mantiene entre 0,15 y 0,75 s; un bloque extra o faltante corrige la deriva
        entre el reloj del RTL-SDR y el de la tarjeta de sonido."""
        self.aplay = subprocess.Popen(['aplay', '-q', '-t', 'raw', '-f', 'S16_LE', '-r', str(FS2), '-c', '1',
                                       '--buffer-time=500000', '--period-time=50000', '-'],
                                      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._log_aplay, daemon=True).start()
        precarga = True
        ultimo = b'\0' * (BLOQUE // D1 // D2 * 2)
        while self.vivo:
            if precarga:
                if self.q_audio.qsize() < 6:
                    time.sleep(0.01)
                    continue
                precarga = False
            try:
                blk = self.q_audio.get(timeout=0.25)
            except queue.Empty:
                blk = ultimo                     # no llego audio a tiempo: repite antes que cortar
                self.stats['cortes'] += 1
                precarga = True
            while self.q_audio.qsize() > 15:
                self.q_audio.get()
                self.stats['ajustes_reloj'] += 1
            ultimo = blk
            try:
                self.aplay.stdin.write(blk)
            except (BrokenPipeError, ValueError):
                return

    def _log_aplay(self):
        for linea in self.aplay.stderr:
            if b'underrun' in linea:
                self.stats['cortes'] += 1


# ---------------------------------------------------------------- interfaz
def interfaz(rx, captura=None):
    import tkinter as tk
    from tkinter import ttk

    raiz = tk.Tk()
    raiz.title('Receptor FM - RTL-SDR')
    raiz.configure(bg='#14161a')
    raiz.geometry('1920x1080+0+0')
    fg, bg, acento = '#e6e6e6', '#14161a', '#3fb6ff'
    W, H = 1840, 225          # pensado para pantalla Full HD 1920x1080

    top = tk.Frame(raiz, bg=bg)
    top.pack(fill='x', padx=12, pady=(10, 0))
    lbl_f = tk.Label(top, text='', font=('DejaVu Sans Mono', 34, 'bold'), fg=acento, bg=bg)
    lbl_f.pack(side='left')
    lbl_est = tk.Label(top, text='', font=('DejaVu Sans Mono', 12), fg=fg, bg=bg, justify='left')
    lbl_est.pack(side='left', padx=18)

    var_f = tk.DoubleVar(value=rx.f / 1e6)
    fila = tk.Frame(raiz, bg=bg)
    fila.pack(fill='x', padx=12)
    tk.Label(fila, text='90 MHz', fg=fg, bg=bg).pack(side='left')
    esc = tk.Scale(fila, from_=F_MIN / 1e6, to=F_MAX / 1e6, resolution=0.1, orient='horizontal', variable=var_f,
                   length=1500, showvalue=False, bg=bg, fg=fg, troughcolor='#2a2e35', highlightthickness=0,
                   command=lambda v: rx.sintonizar(float(v) * 1e6))
    esc.pack(side='left', padx=6)
    tk.Label(fila, text='100 MHz', fg=fg, bg=bg).pack(side='left')

    def paso(df):
        var_f.set(round(np.clip(var_f.get() + df, F_MIN / 1e6, F_MAX / 1e6), 2))
        rx.sintonizar(var_f.get() * 1e6)

    ent = tk.Entry(fila, width=7)
    ent.pack(side='left', padx=(16, 2))

    def ir(_=None):
        try:
            var_f.set(round(float(ent.get().replace(',', '.')), 2))
            rx.sintonizar(var_f.get() * 1e6)
        except ValueError:
            pass
    ent.bind('<Return>', ir)
    ttk.Button(fila, text='Ir', command=ir, width=3).pack(side='left')
    ttk.Button(fila, text='98,0', command=lambda: (var_f.set(98.0), rx.sintonizar(98e6)), width=5).pack(side='left', padx=4)

    fila2 = tk.Frame(raiz, bg=bg)
    fila2.pack(fill='x', padx=12, pady=4)
    tk.Label(fila2, text='Ganancia dB', fg=fg, bg=bg).pack(side='left')
    tk.Scale(fila2, from_=0, to=49.6, resolution=0.1, orient='horizontal', length=180, bg=bg, fg=fg,
             troughcolor='#2a2e35', highlightthickness=0, command=lambda v: rx.poner_ganancia(float(v)),
             variable=tk.DoubleVar(value=rx.ganancia)).pack(side='left')
    tk.Label(fila2, text='Volumen', fg=fg, bg=bg).pack(side='left', padx=(16, 0))
    tk.Scale(fila2, from_=0, to=2, resolution=0.05, orient='horizontal', length=140, bg=bg, fg=fg,
             troughcolor='#2a2e35', highlightthickness=0, variable=tk.DoubleVar(value=rx.volumen),
             command=lambda v: setattr(rx, 'volumen', float(v))).pack(side='left')
    tk.Label(fila2, text='Filtro de canal ± kHz', fg=fg, bg=bg).pack(side='left', padx=(16, 0))
    tk.Scale(fila2, from_=15, to=115, resolution=5, orient='horizontal', length=200, bg=bg, fg=fg,
             troughcolor='#2a2e35', highlightthickness=0, variable=tk.DoubleVar(value=rx.bw_canal / 1e3),
             command=lambda v: rx.poner_filtro(float(v) * 1e3)).pack(side='left')
    ttk.Button(fila2, text='Calibrar con esta estacion', command=rx.calibrar).pack(side='left', padx=16)
    tk.Label(fila2, text='Flechas: ±0,1 MHz   Shift+flechas: ±10 kHz', fg='#8a8f98', bg=bg).pack(side='left')
    raiz.bind('<Left>', lambda e: paso(-0.1))
    raiz.bind('<Right>', lambda e: paso(0.1))
    raiz.bind('<Shift-Left>', lambda e: paso(-0.01))
    raiz.bind('<Shift-Right>', lambda e: paso(0.01))

    panel = tk.Frame(raiz, bg='#1c2026', highlightbackground='#2f353d', highlightthickness=1)
    panel.pack(fill='x', padx=12, pady=(4, 8))
    medida = {}
    for clave, titulo in (('fc', 'Frecuencia central'), ('bw', 'Ancho de banda (98 %)'), ('pot', 'Potencia del canal')):
        caja = tk.Frame(panel, bg='#1c2026')
        caja.pack(side='left', expand=True, fill='x', padx=20, pady=6)
        tk.Label(caja, text=titulo, fg='#8a8f98', bg='#1c2026', font=('DejaVu Sans', 11)).pack(anchor='w')
        medida[clave] = tk.Label(caja, text='—', fg='#ffffff', bg='#1c2026', font=('DejaVu Sans Mono', 24, 'bold'))
        medida[clave].pack(anchor='w')
        medida[clave + '_sub'] = tk.Label(caja, text='', fg='#aab0b8', bg='#1c2026', font=('DejaVu Sans Mono', 11))
        medida[clave + '_sub'].pack(anchor='w')

    cvs = []
    titulos = ['PSD de RF [dB/Hz]: 2,4 MHz alrededor de la estacion (franja azul = canal FM de 200 kHz)',
               'PSD del canal FM [dB/Hz] tras el filtro: ±120 kHz (gris = respuesta del filtro de canal, rojo = ±100 kHz)',
               'PSD del multiplex FM demodulado [dB/Hz]: espectro patron L+R, piloto 19 kHz, L−R y RDS']
    for t in titulos:
        tk.Label(raiz, text=t, fg='#aab0b8', bg=bg, anchor='w', font=('DejaVu Sans', 11)).pack(fill='x', padx=12)
        c = tk.Canvas(raiz, width=W, height=H, bg='#0b0d10', highlightthickness=0)
        c.pack(padx=12, pady=(0, 6))
        cvs.append(c)

    def trazo(c, f, p, fmin, fmax, pmin, pmax, color, marcas, etiq):
        c.delete('all')
        for gx in marcas:
            x = (gx - fmin) / (fmax - fmin) * W
            c.create_line(x, 0, x, H, fill='#22262d')
            c.create_text(x + 2, H - 9, text=etiq(gx), fill='#7b818a', anchor='w', font=('DejaVu Sans', 9))
        for db in np.arange(np.ceil(pmin / 10) * 10, pmax, 10):
            y = H - (db - pmin) / (pmax - pmin) * H
            c.create_line(0, y, W, y, fill='#1b1f25')
            c.create_text(4, y - 7, text=f'{db:.0f} dB/Hz', fill='#5f6670', anchor='w', font=('DejaVu Sans', 9))
        xs = (f - fmin) / (fmax - fmin) * W
        ys = H - (np.clip(p, pmin, pmax) - pmin) / (pmax - pmin) * H
        pts = np.column_stack([xs, ys]).ravel().tolist()
        c.create_line(*pts, fill=color, width=1.5)

    def refrescar():
        if not rx.vivo:
            return
        s = rx.stats
        lbl_f.config(text=f'{rx.f / 1e6:6.2f} MHz')
        cal = 'midiendo…' if rx._calibrando is not None else f'{rx.ppm:+.2f} ppm'
        if rx.aviso:
            cal += '  (' + rx.aviso + ')'
        lbl_est.config(text=(f'Calibracion {cal}   error portadora {s["error_hz"]:+7.0f} Hz\n'
                             f'Señal {s["snr_db"]:4.1f} dB sobre piso   piloto 19 kHz {s["piloto_db"]:+4.1f} dB '
                             f'{"(estereo)" if s["piloto_db"] > 10 else ""}\n'
                             f'Carga DSP {100 * s["carga"]:3.0f} %   cortes de audio {s["cortes"]}   '
                             f'bloques descartados {s["descartados"]}   ajustes reloj {s["ajustes_reloj"]}'))
        md = rx.medidas
        if md:
            medida['fc'].config(text=f'{md["fc"] / 1e6:.4f} MHz')
            medida['fc_sub'].config(text=f'{md["desvio"]:+.0f} Hz respecto a la sintonia')
            medida['bw'].config(text=f'{md["bw"] / 1e3:.1f} kHz')
            medida['bw_sub'].config(text=f'de {md["f1"] / 1e3:+.1f} a {md["f2"] / 1e3:+.1f} kHz   (Carson ≈ 180 kHz)')
            medida['pot'].config(text=f'{md["pot_dbfs"]:.1f} dBFS')
            medida['pot_sub'].config(text=f'{md["pot_ruido"]:+.1f} dB sobre el ruido en el canal')
        else:
            for c in ('fc', 'bw', 'pot'):
                medida[c].config(text='sin señal')
                medida[c + '_sub'].config(text='')
        with rx.lock:
            a, k, m = rx.esp_ancho, rx.esp_canal, rx.esp_mpx
            a = None if a is None else a.copy(); k = None if k is None else k.copy(); m = None if m is None else m.copy()
        def escala(p):
            lo, hi = np.percentile(p, 2) - 2, p.max() + 3
            return lo, max(hi, lo + 15)
        if a is not None:
            fa = (np.arange(2048) - 1024) * FS / 2048 - OFFSET + rx.f
            lo, hi = escala(a[30:-30])
            trazo(cvs[0], fa, a, fa[0], fa[-1], lo, hi, '#7fd3ff',
                  np.arange(np.ceil(fa[0] / 2e5) * 2e5, fa[-1], 2e5), lambda v: f'{v / 1e6:.1f}')
            x0 = (rx.f - rx.bw_canal - fa[0]) / (fa[-1] - fa[0]) * W
            x1 = (rx.f + rx.bw_canal - fa[0]) / (fa[-1] - fa[0]) * W
            cvs[0].create_rectangle(x0, 0, x1, H, outline='', fill='#3fb6ff', stipple='gray25')
        if k is not None:
            fk = (np.arange(1024) - 512) * FS1 / 1024
            lo, hi = escala(k)
            trazo(cvs[1], fk, k, -FS1 / 2, FS1 / 2, lo, hi, '#9dff8a',
                  np.arange(-100e3, 101e3, 25e3), lambda v: f'{v / 1e3:+.0f} k')
            for fx in (-100e3, 100e3):
                x = (fx + FS1 / 2) / FS1 * W
                cvs[1].create_line(x, 0, x, H, fill='#ff6b6b', dash=(4, 3))
            hr = 20 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(rx.f1.h, 1 << 16))) + 1e-9)
            fh = (np.arange(1 << 16) - (1 << 15)) * FS / (1 << 16)
            selh = np.abs(fh) <= FS1 / 2
            ph = hi - 1 + hr[selh]                      # 0 dB del filtro en la parte alta del grafico
            xs = (fh[selh] + FS1 / 2) / FS1 * W
            ys = H - (np.clip(ph, lo, hi) - lo) / (hi - lo) * H
            cvs[1].create_line(*np.column_stack([xs, ys])[::64].ravel().tolist(), fill='#9aa0a8', dash=(3, 2))
            if md:
                x0, x1 = (md['f1'] + FS1 / 2) / FS1 * W, (md['f2'] + FS1 / 2) / FS1 * W
                cvs[1].create_rectangle(x0, 0, x1, H, outline='', fill='#9dff8a', stipple='gray12')
                xc = (md['desvio'] + FS1 / 2) / FS1 * W
                cvs[1].create_line(xc, 0, xc, H, fill='#ffffff', dash=(6, 3))
                cvs[1].create_text(xc + 4, 12, text=f'fc  ·  BW 98 % = {md["bw"] / 1e3:.1f} kHz', fill='#ffffff',
                                   anchor='w', font=('DejaVu Sans', 9))
        if m is not None:
            fm = np.arange(1024) * FS1 / 2048
            sel = fm <= 100e3
            lo, hi = escala(m[sel])
            trazo(cvs[2], fm[sel], m[sel], 0, 100e3, lo, hi, '#ffd36b',
                  np.arange(0, 100e3, 10e3), lambda v: f'{v / 1e3:.0f} kHz')
            for (a0, a1, t) in ((0, 15e3, 'L+R'), (23e3, 53e3, 'L−R (DSB-SC 38 k)'), (55e3, 59e3, 'RDS')):
                x0, x1 = a0 / 100e3 * W, a1 / 100e3 * W
                cvs[2].create_rectangle(x0, 2, x1, 16, outline='#555c66')
                cvs[2].create_text((x0 + x1) / 2, 9, text=t, fill='#c9ced6', font=('DejaVu Sans', 8))
            x = 19e3 / 100e3 * W
            cvs[2].create_line(x, 18, x, H, fill='#ff6b6b', dash=(2, 3))
            cvs[2].create_text(x + 3, 26, text='piloto 19 kHz', fill='#ff8f8f', anchor='w', font=('DejaVu Sans', 8))
        raiz.after(150, refrescar)

    def salir():
        rx.cerrar()
        raiz.destroy()
    raiz.protocol('WM_DELETE_WINDOW', salir)
    refrescar()
    if captura:
        def guardar():
            raiz.update()
            ps = captura + '.ps'
            # el lienzo no incluye los rotulos Tk: se exporta cada espectro por separado
            for i, c in enumerate(cvs):
                c.postscript(file=f'{captura}_{i}.ps', colormode='color')
            json.dump(dict({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rx.stats.items()},
                           medidas=rx.medidas), open(captura + '_stats.json', 'w'))
            salir()
        raiz.after(int(captura_seg * 1000), guardar)
    raiz.mainloop()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-f', type=float, default=98.0, help='frecuencia inicial en MHz (90 a 100)')
    ap.add_argument('-g', type=float, default=49.6, help='ganancia del sintonizador en dB (49,6 = maxima del R820T)')
    ap.add_argument('--prueba', type=float, help='segundos de prueba sin ventana')
    ap.add_argument('--wav', help='guardar el audio en un WAV')
    ap.add_argument('--sin-audio', action='store_true', help='no reproducir por los parlantes')
    ap.add_argument('--calibrar', action='store_true', help='en modo prueba: calibrar con la estacion')
    ap.add_argument('--calibrar-en', type=float, metavar='MHz', help='calibrar con una estacion fuerte (puede estar fuera de 90-100 MHz) y salir')
    ap.add_argument('--captura', help='abrir la ventana, guardar los espectros como PostScript y salir')
    ap.add_argument('--captura-seg', type=float, default=8)
    ap.add_argument('--csv', help='en modo prueba: registrar frecuencia central, ancho de banda y potencia cada 0,5 s')
    ap.add_argument('--filtro', type=float, default=100, help='media banda del filtro de canal en kHz (15 a 115)')
    a = ap.parse_args()
    if a.calibrar_en:
        rx = Receptor(a.calibrar_en * 1e6, a.g, audio=False, libre=True)
        rx.arrancar()
        try:
            time.sleep(2.5)
            if not rx.calibrar(5):
                sys.exit(rx.aviso)
            while rx._calibrando is not None:
                time.sleep(0.2)
            print(f'calibrado con {a.calibrar_en} MHz (SNR {rx.stats["snr_db"]:.1f} dB): {rx.ppm:+.2f} ppm, guardado en {CALIB}')
            time.sleep(2)
            print(f'error restante de portadora: {rx.stats["error_hz"]:+.0f} Hz')
        finally:
            rx.cerrar()
        sys.exit(0)
    if not (F_MIN / 1e6 <= a.f <= F_MAX / 1e6):
        sys.exit(f'la frecuencia debe estar entre {F_MIN / 1e6:.0f} y {F_MAX / 1e6:.0f} MHz')

    w = None
    if a.wav:
        w = wave.open(a.wav, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(FS2)
    rx = Receptor(a.f * 1e6, a.g, audio=not a.sin_audio, wav=w)
    rx.poner_filtro(float(np.clip(a.filtro, 15, 115)) * 1e3)
    rx.arrancar()
    try:
        if a.prueba:
            t0 = time.time()
            if a.calibrar:
                time.sleep(1); rx.calibrar(3)
            reg = open(a.csv, 'w') if a.csv else None
            if reg:
                reg.write('t_s;f_sintonia_MHz;fc_MHz;desvio_Hz;bw98_kHz;f1_kHz;f2_kHz;pot_dBFS;pot_sobre_ruido_dB;snr_dB;piloto_dB;cortes\n')
            while time.time() - t0 < a.prueba:
                time.sleep(0.5)
                s, md = rx.stats, rx.medidas
                if reg and md and time.time() - t0 > 1.5:
                    reg.write(f'{time.time() - t0:.1f};{rx.f / 1e6:.3f};{md["fc"] / 1e6:.5f};{md["desvio"]:.0f};{md["bw"] / 1e3:.2f};'
                              f'{md["f1"] / 1e3:.2f};{md["f2"] / 1e3:.2f};{md["pot_dbfs"]:.2f};{md["pot_ruido"]:.2f};'
                              f'{s["snr_db"]:.2f};{s["piloto_db"]:.2f};{s["cortes"]}\n')
                    reg.flush()
                if int((time.time() - t0) * 2) % 2:
                    continue
                print(f'{time.time() - t0:4.0f}s  bloques {s["bloques"]}  carga {100 * s["carga"]:.0f}%  '
                      f'error {s["error_hz"]:+.0f} Hz  ppm {rx.ppm:+.2f}  SNR {s["snr_db"]:.1f} dB  '
                      f'piloto {s["piloto_db"]:+.1f} dB  cortes {s["cortes"]}  descartados {s["descartados"]}  '
                      f'usb {s["perdidas_usb"]}', flush=True)
            esperado = (time.time() - t0) * FS / BLOQUE
            print(f'bloques recibidos {rx.stats["bloques"]} de ~{esperado:.0f} esperados')
        else:
            captura_seg = a.captura_seg
            interfaz(rx, a.captura)
    finally:
        rx.cerrar()
        if w:
            w.close()
