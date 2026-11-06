import { onBeforeUnmount, onMounted, ref } from "vue";

const MOBILE_BREAKPOINT = 768;

export function useIsMobile() {
  const isMobile = ref(false);
  let mql: MediaQueryList | null = null;

  const onChange = () => {
    isMobile.value = window.innerWidth < MOBILE_BREAKPOINT;
  };

  onMounted(() => {
    mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
    mql.addEventListener("change", onChange);
    onChange();
  });

  onBeforeUnmount(() => {
    mql?.removeEventListener("change", onChange);
  });

  return isMobile;
}
