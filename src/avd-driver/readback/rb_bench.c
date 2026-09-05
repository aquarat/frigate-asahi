// Read-back bandwidth benchmark for one AVD capture buffer (NV12 2304x1296).
// Usage: sudo ./rb_bench [noncoherent] [iters]
// Allocates 1 CAPTURE buffer via REQBUFS(MMAP), maps it both via mmap(video fd)
// and via the exported dma-buf, then measures copy/read bandwidth with several
// methods. No decode is performed (buffer content is irrelevant for bandwidth).
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <arm_neon.h>
#include <linux/videodev2.h>
#include <linux/dma-buf.h>

#ifndef V4L2_MEMORY_FLAG_NON_COHERENT
#define V4L2_MEMORY_FLAG_NON_COHERENT (1 << 0)
#endif
#ifndef V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS
#define V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS (1 << 6)
#endif

static double now(void){struct timespec ts;clock_gettime(CLOCK_MONOTONIC,&ts);return ts.tv_sec+ts.tv_nsec*1e-9;}
static int xioctl(int fd, unsigned long req, void *arg, const char *name){int r=ioctl(fd,req,arg); if(r<0){fprintf(stderr,"%s: %s\n",name,strerror(errno));} return r;}

// NEON 64-byte-block copy using 4x16B loads (ld1 x4) and stores
static void neon_copy(uint8_t *dst, const uint8_t *src, size_t n){
    size_t i=0;
    for(; i+64<=n; i+=64){ uint8x16x4_t v=vld1q_u8_x4(src+i); vst1q_u8_x4(dst+i,v); }
    if(i<n) memcpy(dst+i,src+i,n-i);
}
// NEON 128-byte block, two independent 64B loads in flight
static void neon_copy2(uint8_t *dst, const uint8_t *src, size_t n){
    size_t i=0;
    for(; i+128<=n; i+=128){ uint8x16x4_t a=vld1q_u8_x4(src+i); uint8x16x4_t b=vld1q_u8_x4(src+i+64); vst1q_u8_x4(dst+i,a); vst1q_u8_x4(dst+i+64,b); }
    if(i<n) memcpy(dst+i,src+i,n-i);
}
// NEON 256-byte block, four independent 64B loads
static void neon_copy4(uint8_t *dst, const uint8_t *src, size_t n){
    size_t i=0;
    for(; i+256<=n; i+=256){ uint8x16x4_t a=vld1q_u8_x4(src+i); uint8x16x4_t b=vld1q_u8_x4(src+i+64); uint8x16x4_t c=vld1q_u8_x4(src+i+128); uint8x16x4_t d=vld1q_u8_x4(src+i+192);
      vst1q_u8_x4(dst+i,a); vst1q_u8_x4(dst+i+64,b); vst1q_u8_x4(dst+i+128,c); vst1q_u8_x4(dst+i+192,d); }
    if(i<n) memcpy(dst+i,src+i,n-i);
}
// LDNP-based copy (non-temporal pair loads), 64B per iteration
static void ldnp_copy(uint8_t *dst, const uint8_t *src, size_t n){
    size_t i=0;
    for(; i+64<=n; i+=64){
        uint64_t a,b,c,d,e,f,g,h;
        __asm__ volatile("ldnp %0, %1, [%8]\n\tldnp %2, %3, [%8, #16]\n\tldnp %4, %5, [%8, #32]\n\tldnp %6, %7, [%8, #48]"
            : "=&r"(a),"=&r"(b),"=&r"(c),"=&r"(d),"=&r"(e),"=&r"(f),"=&r"(g),"=&r"(h) : "r"(src+i) : "memory");
        __asm__ volatile("stnp %0, %1, [%8]\n\tstnp %2, %3, [%8, #16]\n\tstnp %4, %5, [%8, #32]\n\tstnp %6, %7, [%8, #48]"
            : : "r"(a),"r"(b),"r"(c),"r"(d),"r"(e),"r"(f),"r"(g),"r"(h),"r"(dst+i) : "memory");
    }
    if(i<n) memcpy(dst+i,src+i,n-i);
}
static uint64_t sum_read(const uint8_t *src, size_t n){ // read-only, 64-bit adds
    const uint64_t *p=(const uint64_t*)src; uint64_t s=0; for(size_t i=0;i<n/8;i++) s+=p[i]; return s; }

typedef void (*copyfn)(uint8_t*,const uint8_t*,size_t);
static void bench(const char *name, copyfn fn, uint8_t *dst, const uint8_t *src, size_t n, int iters){
    fn(dst,src,n); // warm
    double t0=now(); for(int i=0;i<iters;i++) fn(dst,src,n); double t=now()-t0;
    printf("  %-28s %8.2f ms/frame  %8.0f MB/s\n", name, t/iters*1e3, n/(t/iters)/1e6);
}

int main(int argc,char**argv){
    int noncoh = argc>1 && !strcmp(argv[1],"noncoherent");
    int iters = argc>2 ? atoi(argv[2]) : 20;
    int fd=open("/dev/video0",O_RDWR|O_NONBLOCK); if(fd<0){perror("open /dev/video0");return 1;}
    struct v4l2_format f={0};
    f.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE; f.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_H264_SLICE;
    f.fmt.pix_mp.width=2304; f.fmt.pix_mp.height=1296; f.fmt.pix_mp.num_planes=1; f.fmt.pix_mp.plane_fmt[0].sizeimage=2<<20;
    if(xioctl(fd,VIDIOC_S_FMT,&f,"S_FMT(out)")<0) return 1;
    struct v4l2_format c={0}; c.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
    if(xioctl(fd,VIDIOC_G_FMT,&c,"G_FMT(cap)")<0) return 1;
    printf("capture: %ux%u fourcc %.4s planes %u sizeimage %u bpl %u\n", c.fmt.pix_mp.width,c.fmt.pix_mp.height,(char*)&c.fmt.pix_mp.pixelformat,c.fmt.pix_mp.num_planes,c.fmt.pix_mp.plane_fmt[0].sizeimage,c.fmt.pix_mp.plane_fmt[0].bytesperline);
    struct v4l2_requestbuffers rb={0}; rb.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; rb.memory=V4L2_MEMORY_MMAP; rb.count=1;
    rb.flags = noncoh ? V4L2_MEMORY_FLAG_NON_COHERENT : 0;
    if(xioctl(fd,VIDIOC_REQBUFS,&rb,"REQBUFS")<0) return 1;
    printf("reqbufs: count %u caps 0x%x (%s) flags-after 0x%x  requested-noncoherent=%d\n", rb.count, rb.capabilities,
        (rb.capabilities & V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS) ? "MMAP_CACHE_HINTS supported" : "no cache hints", rb.flags, noncoh);
    struct v4l2_plane planes[VIDEO_MAX_PLANES]={0}; struct v4l2_buffer b={0};
    b.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; b.memory=V4L2_MEMORY_MMAP; b.index=0; b.m.planes=planes; b.length=VIDEO_MAX_PLANES;
    if(xioctl(fd,VIDIOC_QUERYBUF,&b,"QUERYBUF")<0) return 1;
    size_t n=planes[0].length; printf("plane0 length %zu offset %u\n", n, planes[0].m.mem_offset);
    uint8_t *src=mmap(NULL,n,PROT_READ|PROT_WRITE,MAP_SHARED,fd,planes[0].m.mem_offset); if(src==MAP_FAILED){perror("mmap video");return 1;}
    struct v4l2_exportbuffer eb={0}; eb.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE; eb.index=0; eb.plane=0; eb.flags=O_RDONLY;
    if(xioctl(fd,VIDIOC_EXPBUF,&eb,"EXPBUF")<0) return 1;
    uint8_t *src2=mmap(NULL,n,PROT_READ,MAP_SHARED,eb.fd,0); if(src2==MAP_FAILED){perror("mmap dmabuf");return 1;}
    uint8_t *dst=aligned_alloc(4096,n); memset(dst,1,n);
    uint8_t *ram=aligned_alloc(4096,n); memset(ram,2,n);
    // touch pages
    memset(src,0,n);
    printf("== source: mmap(/dev/video0) buffer, %zu bytes, iters %d\n", n, iters);
    bench("memcpy", (copyfn)memcpy, dst, src, n, iters);
    bench("neon 64B", neon_copy, dst, src, n, iters);
    bench("neon 128B", neon_copy2, dst, src, n, iters);
    bench("neon 256B", neon_copy4, dst, src, n, iters);
    bench("ldnp/stnp 64B", ldnp_copy, dst, src, n, iters);
    { double t0=now(); volatile uint64_t s=0; for(int i=0;i<iters;i++) s+=sum_read(src,n); double t=now()-t0; printf("  %-28s %8.2f ms/frame  %8.0f MB/s\n","read-only u64 sum", t/iters*1e3, n/(t/iters)/1e6); }
    printf("== source: mmap(dma-buf fd) (what ffmpeg uses)\n");
    { struct dma_buf_sync s={.flags=DMA_BUF_SYNC_START|DMA_BUF_SYNC_READ}; double t0=now(); for(int i=0;i<iters;i++){ ioctl(eb.fd,DMA_BUF_IOCTL_SYNC,&s);} printf("  %-28s %8.3f ms\n","DMA_BUF_IOCTL_SYNC start", (now()-t0)/iters*1e3); }
    bench("memcpy", (copyfn)memcpy, dst, src2, n, iters);
    bench("neon 256B", neon_copy4, dst, src2, n, iters);
    printf("== reference: plain RAM -> RAM (cached)\n");
    bench("memcpy", (copyfn)memcpy, dst, ram, n, iters);
    bench("neon 256B", neon_copy4, dst, ram, n, iters);
    // per-frame mmap/munmap cost of the dma-buf (ffmpeg maps per frame)
    { double t0=now(); for(int i=0;i<iters;i++){ uint8_t *m=mmap(NULL,n,PROT_READ,MAP_SHARED,eb.fd,0); if(m==MAP_FAILED){perror("mmap");break;} volatile uint8_t x=m[0];(void)x; munmap(m,n);} printf("  %-28s %8.3f ms\n","dma-buf mmap+touch+munmap", (now()-t0)/iters*1e3); }
    return 0;
}
