/* Freestanding AArch64 Linux probe. Runs only INSIDE the disposable guest.
 * UAPI definitions are from the SDK's Linux kvm.h, not host userspace APIs. */
#include <stddef.h>
#include <linux/kvm.h>
static long sys6(long n,long a,long b,long c,long d,long e,long f) {
 register long x8 __asm__("x8")=n, x0 __asm__("x0")=a, x1 __asm__("x1")=b,
 x2 __asm__("x2")=c, x3 __asm__("x3")=d, x4 __asm__("x4")=e, x5 __asm__("x5")=f;
 __asm__ volatile("svc 0":"+r"(x0):"r"(x8),"r"(x1),"r"(x2),"r"(x3),"r"(x4),"r"(x5):"memory"); return x0;
}
static void say(const char *s) { long n=0; while(s[n])n++; sys6(64,1,(long)s,n,0,0,0); }
static void die(const char *s) { say(s); sys6(93,1,0,0,0,0,0); for(;;){} }
static long io(long fd,long cmd,long arg) { return sys6(29,fd,cmd,arg,0,0,0); }
static void reg(long cpu, unsigned long long id, unsigned long long value) {
 struct kvm_one_reg r={.id=id,.addr=(unsigned long)&value};
 if(io(cpu,KVM_SET_ONE_REG,(long)&r)<0)die("FAIL SET_ONE_REG\n");
}
static unsigned int code[1024] __attribute__((aligned(4096)))={
 0xd2a20000, /* mov x0, #0x10000000: unmapped MMIO address */
 0xd2800541, /* mov x1, #42 */
 0xf9000001, /* str x1, [x0] */
 0x14000000  /* b . */
};
void _start(void) {
 long kvm=sys6(56,-100,(long)"/dev/kvm",2,0,0,0);
 if(kvm<0)die("FAIL open KVM\n");
 if(io(kvm,KVM_GET_API_VERSION,0)!=12)die("FAIL API_VERSION\n"); say("PASS KVM_API_VERSION=12\n");
 long vm=io(kvm,KVM_CREATE_VM,0); if(vm<0)die("FAIL CREATE_VM\n"); say("PASS CREATE_VM\n");
 struct kvm_userspace_memory_region mem={.slot=0,.guest_phys_addr=0x40000000,.memory_size=4096,.userspace_addr=(unsigned long)code};
 if(io(vm,KVM_SET_USER_MEMORY_REGION,(long)&mem)<0)die("FAIL MEMORY_REGION\n");
 long cpu=io(vm,KVM_CREATE_VCPU,0); if(cpu<0)die("FAIL CREATE_VCPU\n");
 struct kvm_vcpu_init init={0};
 if(io(vm,KVM_ARM_PREFERRED_TARGET,(long)&init)<0)die("FAIL PREFERRED_TARGET\n");
 if(io(cpu,KVM_ARM_VCPU_INIT,(long)&init)<0)die("FAIL VCPU_INIT\n"); say("PASS VCPU_INIT\n");
 unsigned long long base=KVM_REG_ARM64|KVM_REG_SIZE_U64|KVM_REG_ARM_CORE;
 reg(cpu,base|KVM_REG_ARM_CORE_REG(regs.pc),0x40000000);
 reg(cpu,base|KVM_REG_ARM_CORE_REG(regs.pstate),0x3c5);
 long size=io(kvm,KVM_GET_VCPU_MMAP_SIZE,0);
 if(size<0)die("FAIL MMAP_SIZE\n");
 long addr=sys6(222,0,size,3,1,cpu,0); if(addr<0)die("FAIL mmap\n");
 struct kvm_run *run=(void *)addr;
 if(io(cpu,KVM_RUN,0)<0)die("FAIL KVM_RUN\n");
 if(run->exit_reason!=KVM_EXIT_MMIO || run->mmio.phys_addr!=0x10000000 || !run->mmio.is_write || run->mmio.len!=8 || run->mmio.data[0]!=42)die("FAIL MMIO result\n");
 say("PASS KVM_RUN: L2 executed store(42), KVM_EXIT_MMIO\n");
 sys6(93,0,0,0,0,0,0); for(;;){}
}
