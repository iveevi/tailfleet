"""Shell snippets piped to `bash -s` on each node."""

STATIC_PROBE = r"""
emit() { printf '%s\t%s\n' "$1" "$2"; }
emit CPU_MODEL  "$(lscpu | sed -n 's/^Model name:[[:space:]]*//p' | head -1)"
emit CORES      "$(nproc)"
emit THREADS    "$(lscpu | sed -n 's/^CPU(s):[[:space:]]*//p' | head -1)"
emit MHZ_MAX    "$(lscpu | sed -n 's/^CPU max MHz:[[:space:]]*//p' | head -1)"
emit MEM_KB     "$(awk '/MemTotal/{print $2}' /proc/meminfo)"
emit ARCH       "$(uname -m)"
emit KERNEL     "$(uname -r)"
emit OS_NAME    "$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version \
    --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r line; do
      emit GPU_STATIC "$line"
    done
else
  lspci 2>/dev/null | grep -iE 'vga|3d|display' | sed 's/^[^ ]* //' | while IFS= read -r line; do
    emit GPU_PCI "$line"
  done
fi
"""

DYNAMIC_PROBE = r"""
emit() { printf '%s\t%s\n' "$1" "$2"; }
cpu_sample() { awk '/^cpu /{idle=$5+$6; tot=0; for(i=2;i<=NF;i++) tot+=$i; print tot, idle}' /proc/stat; }
read t1 i1 < <(cpu_sample); sleep 0.1; read t2 i2 < <(cpu_sample)
emit CPU_UTIL    "$(awk -v t1=$t1 -v i1=$i1 -v t2=$t2 -v i2=$i2 'BEGIN{dt=t2-t1;di=i2-i1; if(dt>0) printf "%.0f",(1-di/dt)*100; else printf "0"}')"
emit MEM_USED_KB "$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{print t-a}' /proc/meminfo)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=memory.used,utilization.gpu,temperature.gpu \
    --format=csv,noheader,nounits 2>/dev/null | while IFS= read -r line; do
      emit GPU_DYN "$line"
    done
elif lspci 2>/dev/null | grep -iE 'vga|3d|display' | grep -qi intel; then
  ig=""
  command -v intel_gpu_top >/dev/null 2>&1 && ig="$(timeout 1.2 intel_gpu_top -J -s 500 2>/dev/null | base64 | tr -d '\n')"
  if [ -n "$ig" ]; then
    emit INTEL_B64 "$ig"
  elif command -v gputop >/dev/null 2>&1; then
    gt="$(timeout 2.5 gputop -n 2 -d 0.3 2>/dev/null | base64 | tr -d '\n')"
    [ -n "$gt" ] && emit GPUTOP_B64 "$gt"
  fi
fi
"""

PROBE = STATIC_PROBE + DYNAMIC_PROBE
