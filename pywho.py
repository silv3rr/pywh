#!/usr/bin/env python3

# pylint: disable=line-too-long, consider-using-f-string

"""
################################################################################
# PY-WHO!? pzs-ng's sitewho ported to Python                             # slv #
################################################################################
# Uses SHM and glftpd's 'ONLINE' C struct, module sysv_ipc is required         #
# See README and comments in pywho.conf for install and config instructions    #
################################################################################
"""

import string
import struct
import re
import time
import datetime
import configparser
import os
import sys
import socket
import calendar
import collections
import sysv_ipc

VERSION = "20230512"

# vars used like #ifdef's in orig sitewho.c
_WITH_ALTWHO = True
_WITH_SS5 = False
_WITH_GEOIP = False
_WITH_XXL = True

SCRIPT = os.path.basename(sys.argv[0])
SCRIPTDIR = os.path.dirname(os.path.realpath((sys.argv[0])))
SCRIPTNAME = os.path.splitext(SCRIPT)[0]

if not _WITH_SS5:
    RAW_OUTPUT = 0
    USER_IDX = 1
else:
    RAW_OUTPUT = 2
    USER_IDX = 2
UPLOADS = DOWNLOADS = 0
TOTAL_UP_SPEED = TOTAL_DN_SPEED = 0
ONLINEUSERS = BROWSERS = IDLERS = 0
SHOWALL = 0
GEOIP2_BUF = {}
GEOIP2_CLIENT = None

GL_NOCOLOR = 0
XXL_MODE = 0
HTML_MODE=0


# handle args
##############
if '-h' in sys.argv or '--help' in sys.argv:
    print(f'./{SCRIPTNAME} [--raw|-ss5|--nbw] [username]')
    sys.exit(0)
elif '-v' in sys.argv or '--version' in sys.argv:
    ver = f"pypwho-{VERSION}"
    if _WITH_ALTWHO:
        ver += '-altwho'
    if _WITH_GEOIP:
        ver += '-geoip'
    if _WITH_XXL:
        ver += '-xxl'
    print(ver)
    sys.exit(0)
elif len(sys.argv) > 1 and len(sys.argv[1]) == 5:
    if '--raw' in sys.argv:
        USER_IDX, RAW_OUTPUT = 2, 1
    elif '--ss5' in sys.argv:
        USER_IDX, RAW_OUTPUT = 2, 2
    elif '--nbw' in sys.argv:
        USER_IDX, RAW_OUTPUT = 2, 3
    elif '--spy' in sys.argv:
        print("Mode was moved to a separate tool called 'spy.py'")
        sys.exit(0)
    elif '--xxl' in sys.argv:
        if _WITH_XXL:
            USER_IDX, RAW_OUTPUT = 2, 0
            XXL_MODE = 1
        else:
            sys.exit(0)
else:
    if len(sys.argv) > 1 and sys.argv[1][0] == '-':
        print("Error: invalid option, try '-h'\n")
        sys.exit(1)


# config file
##############

CONFIGFILE = f'{SCRIPTDIR}/{SCRIPTNAME}.conf'
config = configparser.ConfigParser()
file_errors = []
for cfg in set([CONFIGFILE, f'{SCRIPTDIR}/pywho.conf']):
    try:
        with open(cfg, 'r', encoding='utf-8', errors='ignore') as f:
            config.read_string("[DEFAULT]\n" + f.read())
    except IOError as cf_err:
        file_errors.append(cf_err)
if len(file_errors) > 0:
    for f_err in file_errors:
        print(f_err)
    print('Error: opening config file')
    sys.exit(1)

layout = {}
tmpl_str = {}
tmpl_sub = {}
default = {
    'header':       ".-[PY-WHO]--------------------------------------------------------------.",
    'footer':       "`------------------------------------------------------------[PY-WHO]---'",
    'separator':    " -----------------------------------------------------------------------"
}
tls_mode = [
    0, 'None',       # no ssl
    1, 'Control',    # ssl on control
    2, 'Both'        # ssl on control and data
]

try:
    glrootpath = config['DEFAULT']['glrootpath']
    headerfile = config['DEFAULT']['headerfile']
    footerfile = config['DEFAULT']['footerfile']
    separatorfile = config['DEFAULT']['separatorfile']
    husers = config.get('DEFAULT', 'hiddenusers', fallback='')
    hgroups = config.get('DEFAULT', 'hiddengroups', fallback='')
    mpaths = config.get('DEFAULT', 'maskeddirectories', fallback='')
    ipc_key = config.get('DEFAULT', 'ipc_key', fallback='')
    seeallflags = config.get('DEFAULT', 'seeallflags', fallback='')
    maxusers = config.getint('DEFAULT', 'maxusers', fallback=20)
    nocase = config.getboolean('DEFAULT', 'case_insensitive', fallback=False)
    count_hidden = config.getboolean('DEFAULT', 'count_hidden', fallback=True)
    idle_barrier = config.getint('DEFAULT', 'idle_barrier', fallback=30)
    threshold = config.getint('DEFAULT', 'speed_threshold', fallback=1024)
    color = config.getint('DEFAULT', 'color', fallback=1)
    debug = config.getint('DEFAULT', 'debug', fallback=0)
    geoip2_enable = config.getboolean('GEOIP', 'geoip2_enable', fallback=False)
    geoip2_accountid = config['GEOIP']['geoip2_accountid']
    geoip2_licensekey = config['GEOIP']['geoip2_licensekey']
    geoip2_proxy = config.get('GEOIP', 'geoip2_proxy', fallback=None)
    layout['header'] = config.get('THEME', 'header', fallback=default['header'])
    layout['footer'] = config.get('THEME', 'footer', fallback=default['footer'])
    layout['separator'] = config.get('THEME', 'separator', fallback=default['separator'])
    tmpl_str['upload'] = config['THEME']['template_upload']
    tmpl_str['download'] = config['THEME']['template_download']
    tmpl_str['info'] = config['THEME']['template_info']
    tmpl_str['totals'] = config['THEME']['template_totals']
    tmpl_str['users'] = config['THEME']['template_users']
    tmpl_sub['hrchar'] = config.get('THEME', 'hrchar', fallback=':')
    tmpl_sub['delimiter'] = config.get('THEME', 'delimiter', fallback='|')
    emoji = config.getboolean('THEME', 'emoji', fallback=False)
except (KeyError, configparser.InterpolationError) as conf_err:
    print(f'ERROR: check config file\n{conf_err}')
    sys.exit(1)

CHIDDEN = 1 if count_hidden else 0
MAXUSERS = maxusers if maxusers else 0


# glftpd data
##############

# shm and struct (default ipc_key: 0x0000dead=57005)
IPC_KEY = ipc_key if ipc_key else "0x0000DEAD"
KEY = int(IPC_KEY, 16)
# NULL_CHAR = '\0'
NULL_CHAR = b'\x00'
if debug > 3:
    print(f"DEBUG:\tIPC_KEY={IPC_KEY} KEY={KEY} sysv_ipc.SHM_RDONLY={sysv_ipc.SHM_RDONLY}\n",
          f'\tfmt = {KEY:#010x}', id(KEY))

# converted from structonline.h and arranged like struct_ONLINE below:
# tag(64s) username(24s) status(h) ... procid(i)
STRUCT_FMT = ' \
  64s  24s  256s  h  256s  256s  i  i  \
  2i \
  2i \
  2I \
  2I \
  i  \
'

# pylint: disable=invalid-name
struct_ONLINE = collections.namedtuple(
    'struct_ONLINE',
    'tagline username status ssl_flag host currentdir groupid login_time \
    tstart_tv_sec tstart_tv_usec  \
    txfer_tv_sec txfer_tv_usec    \
    bytes_xfer1 bytes_xfer2       \
    bytes_txfer1 bytes_txfer2     \
    procid'
)

try:
    for cfg_name in seeallflags:
        if cfg_name in os.getenv("FLAGS"):
            SHOWALL = 1
            break
    if '5' not in os.getenv("FLAGS"):
        GL_NOCOLOR = 1
except (TypeError, KeyError, OSError):
    pass

try:
    with open(f'{glrootpath}/etc/group', 'r', encoding='utf-8', errors='ignore') as f:
        groupfile = f.readlines()
except IOError:
    with open('/etc/group', 'r', encoding='utf-8', errors='ignore') as f:
        groupfile = f.readlines()


# geoip
########

if _WITH_GEOIP and geoip2_enable:
    import geoip2.webservice
    GEOIP2_CLIENT = geoip2.webservice.Client(
        geoip2_accountid,
        geoip2_licensekey, host='geolite.info',
        proxy=None if not geoip2_proxy or geoip2_proxy == 'None' else geoip2_proxy
    )


# theme
########

mode_list = []
layout_keys   = ['header', 'footer', 'separator']
tmpl_str_keys = ['upload', 'download', 'info', 'totals', 'users']

# try config keys 'header' and 'footer' etc first, fallback to header/footerfile
for k in layout_keys:
    try:
        layout[k]
    except KeyError as e:
        print(f"Theme setting not found, trying file instead... (error: {e})")
        try:
            tf = config.get('DEFAULT', f'{k}file')
            with open(f'{glrootpath}{tf}', 'r', encoding='utf-8', errors='ignore') as f:
                layout[k] = f.read().strip()
        except (KeyError, IOError) as t_err:
            print(f"File not found for theme '{k}' (error: {t_err})")

# for xxl mode get and replace theme keys
if _WITH_XXL and XXL_MODE:
    mode_list.append('xxl')
for m in mode_list:
    for k in layout_keys:
        layout[k] = config.get('THEME', f'{m}_{k}', fallback=default[k])
    for k in tmpl_str_keys:
        tmpl_str[k] = config.get('THEME', f'template_{m}_{k}', fallback=config['THEME'][f'template_{k}'])

# use unicode for layout and template keys to make sure we output ansi escapes
for k in layout_keys:
    layout[k] = layout[k].encode().decode('unicode-escape')
for k in tmpl_str_keys:
    tmpl_str[k] = tmpl_str[k].encode().decode('unicode-escape')

# strip colors from output if running from gl and '5' is not in FLAGS, or color=0, or xxlmode
if color == 0 or XXL_MODE:
    re_esc = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')
    for k in layout_keys:
        layout[k] = re_esc.sub('', layout[k])
    for k in tmpl_str_keys:
        tmpl_str[k] = re_esc.sub('', tmpl_str[k])


# functions
############

def glconf_users():
    """ sum max_users from glftpd.conf """
    u_cnt = 0
    for cfg_fname in [f'{glrootpath}/../glftpd.conf', f'{glrootpath}/glftpd.conf', '/etc/glftpd.conf']:
        try:
            with open(cfg_fname, 'r', encoding='utf-8', errors='ignore') as cf:
                for line in cf.readlines():
                    if re.search(r'^max_users \d', line):
                        for i in line.split()[1:]:
                            u_cnt += int(i)
                        break
        except IOError:
            pass
    return u_cnt


def get_group(gid):
    """ get group name using gid """
    line = None
    for line in groupfile:
        if line.split(':')[2] == str(gid):
            g_name = line.split(':')[0]
            return g_name
    return None


def get_gid(g_name):
    """ get gid using group name """
    line = None
    gid = 0
    for line in groupfile:
        if line.split(':')[0] == g_name:
            gid = line.split(':')[2]
            return gid
    return 0


def filesize(filename):
    """ get filesize in bytes """
    for file in filename, f'{glrootpath}{filename}':
        try:
            return os.path.getsize(file)
        except OSError:
            pass
    return 0

def cprint(message):
    """ format max columns """
    print("{msg:<{col}.{col}}".format(
        msg=message, col=os.get_terminal_size().columns)
    )


def get_geocode(client, userip, shown_err):
    """ get geoip2 country code for ip """
    iso_code = "xX"
    if debug > 0:
        for _ in ['127.', '10.', '172.16.1', '172.16.2', '172.16.3', '192.168.']:
            if userip.startswith(_):
                if debug > 3:
                    print(f'DEBUG: geoip2 MATCH {_} in {userip}')
                return [ client, 'DEBUG', shown_err ]
    if GEOIP2_BUF.get(userip):
        iso_code = GEOIP2_BUF[userip]
    else:
        try:
            if debug == 1:
                print('DEBUG: got cached GEOIP2_BUF[userip]', GEOIP2_BUF[userip])
            iso_code = client.country(userip).country.iso_code
            GEOIP2_BUF[userip] = iso_code
        except geoip2.errors.GeoIP2Error as g_err:
            # var shown_err makes sure we only show the error once
            if (g_err.__class__.__name__ in ['AddressNotFoundError', 'reqOutOfQueriesError']) and shown_err == 0:
                shown_err = 1
                _m = f'Error: geoip2 {g_err.__class__.__name__} ({g_err})'
                if _WITH_XXL and XXL_MODE:
                    print(_m, '\n')
                else:
                    print('\n'.join(_.strip() for _ in re.findall(r'.{1,75}(?:\s+|$)', _m)))
    return [ client, iso_code, shown_err ]


def showusers(user, *args, **kwargs) -> dict:
    """ output formatted user stats """
    # set variables from function parameters
    mode = args[0]
    ucomp = args[1]
    raw = args[2]
    x = args[3]
    chidden = args[4]
    downloads = kwargs['downloads']
    uploads = kwargs['uploads']
    total_up_speed = kwargs['total_up_speed']
    total_dn_speed = kwargs['total_dn_speed']
    browsers = kwargs['browsers']
    idlers = kwargs['idlers']
    onlineusers = kwargs['onlineusers']
    geoip2_client = kwargs['geoip2_client']
    geoip2_shown_err = kwargs['geoip2_shown_err']
    # convert 2 uint32 to uint64
    bytes_xfer = user[x].bytes_xfer2 * pow(2, 32) + user[x].bytes_xfer1

    # XXX: to test total up/dn speed set vars like this:
    #        uploads, downloads, total_up_speed, total_dn_speed = 10, 3, 18576, 8576   # 1048576 (1024*1024)
    #      examples of 'status' output:
    #        b'STOR filename'
    #        b'LIST -al\x00partof-DIR\x003/0504/TEST2\x00/Foo-BAR/1'
    #        b'RETR filename.rar\x00X/',
    #        b'STAT'
    #        b'PASV'
    #        b'Connecting...'
    #      (OLD) glftpd 2.11: username = user[x].username.decode().split(NULL_CHAR, 1)[0]

    username = user[x].username.split(NULL_CHAR, 1)[0].decode()
    tagline = user[x].tagline.split(NULL_CHAR, 1)[0].decode()
    currentdir = user[x].currentdir.split(NULL_CHAR, 1)[0].decode()
    # tagline = user[x].tagline.split(NULL_CHAR, 1)[0].decode()
    u_status = user[x].status.split(NULL_CHAR, 1)[0].decode()
    tstop_tv_sec = calendar.timegm(time.gmtime())
    tstop_tv_usec = datetime.datetime.now().microsecond
    host = g_name = traf_dir = None
    speed = pct = mask = noshow = 0
    maskchar = " "
    p_bar = ""
    userip = '0.0.0.0'

    # skip if host is empty
    if user[x].host != b'':
        host = user[x].host.split(NULL_CHAR, 1)[0].decode()
        (_, addr) = host.split('@', 2)[0:2]
        # ipv4/6
        # (OLD) if re.search(r'([\d.]{7,}|:)', addr):
        if (''.join((addr).split('.', 3)).isdigit()) or (':' in addr):
            userip = addr
        # addr is not a fqdn
        elif '.' not in addr:
            userip = '127.0.0.1' if addr == 'localhost' else '0.0.0.0'
        else:
            try:
                userip = socket.gethostbyname(addr)
            except OSError:
                pass

    if len(u_status) > 5 and not u_status[5:].startswith('-'):
        filename = u_status[5:]
    else:
        filename = ''

    if user[x].groupid >= 0:
        g_name = get_group(user[x].groupid) if get_group(user[x].groupid) else ""

    # check if user in hidden users/groups
    if ((nocase and ((username.lower() in husers.lower()) or (g_name.lower() in hgroups.lower()))) or
            ((username in husers) or (g_name in hgroups))):
        if SHOWALL:
            maskchar = '*'
        else:
            noshow += 1

    if noshow == 0 and mpaths:
        if ((maskchar == '') and (currentdir in mpaths.split(' ') or (f'{currentdir}/' in mpaths.split(' ')))):
            if SHOWALL:
                maskchar = '*'
            else:
                mask += 1

    if _WITH_GEOIP and geoip2_enable:
        (geoip2_client, iso_code, geoip2_shown_err) = get_geocode(geoip2_client, userip, geoip2_shown_err)
        userip = f'{userip} {iso_code}' if (userip and iso_code) else userip

    # NOTE: when testing bytes_xfer1, use replace since namedtuple is immutable:
    #       user[x] = user[x]._replace(bytes_xfer1=150000)

    # ul speed
    if (user[x].status[:5] == b'STOR ' or user[x].status[:5] == b'APPE ') and bytes_xfer:
        mb_xfered = abs(bytes_xfer / 1024 / 1024)
        traf_dir = "Up"
        speed = abs(
            bytes_xfer / 1024 / ((tstop_tv_sec - user[x].tstart_tv_sec) +
            (tstop_tv_usec - user[x].tstart_tv_usec) / 1000000)
        )
        if (not noshow and not mask and maskchar != '*') or chidden:
            total_up_speed += speed
            uploads += 1
        if not mask:
            pct = -1
            p_bar = '?->'
    # dn speed
    elif user[x].status[:5] == b'RETR ' and bytes_xfer:
        mb_xfered = 0
        traf_dir = "Dn"
        realfile = currentdir
        my_filesize = filesize(realfile)
        if my_filesize < bytes_xfer:
            my_filesize = bytes_xfer
        pct = abs(
            bytes_xfer / my_filesize * 100
        )
        i = 15 * bytes_xfer / my_filesize
        i = 15 if i > 15 else i
        # for _ in range(0, int(i)): p_bar += 'x'
        # x = 'x' * len(range(0, int(i)))
        p_bar = f"{'':x<{int(abs(i))}}"
        speed = abs(
            bytes_xfer / 1024 / ((tstop_tv_sec - user[x].tstart_tv_sec) +
            (tstop_tv_usec - user[x].tstart_tv_usec) / 1000000)
        )
        if (not noshow and not mask and maskchar != '*') or chidden:
            total_dn_speed += speed
            downloads += 1
    # idle time
    else:
        p_bar = filename = ""
        pct = mb_xfered = 0
        seconds = tstop_tv_sec - user[x].tstart_tv_sec
        if ((not noshow and not mask and maskchar != '*') and chidden):
            if seconds > idle_barrier:
                idlers += 1
            else:
                browsers += 1
        if not raw:
            status = 'Idle: {:>9.9}'.format(time.strftime("%H:%M:%S", time.gmtime(seconds)))
        elif raw == 1:
            status = '"ID" {}'.format(time.strftime("%S", time.gmtime(seconds)))
        else:
            status = 'idle|{}'.format(time.strftime("%H|%M|%S", time.gmtime(seconds)))

    user.online = '{}'.format(time.strftime("%H:%M:%S", time.gmtime(tstop_tv_sec - user[x].login_time)))

    # format both Up/Dn speed to KB/s MB/s GB/s
    if speed and (traf_dir == "Up" or traf_dir == "Dn"):
        if not mask and not raw:
            # filename = '{:<.{prec}}'.format(filename, prec=int(m))
            if len(filename) > 15:
                filename = f'{filename:<.15}'
            if speed > (threshold * threshold):
                status = '{}: {:7.2f}GB/s'.format(traf_dir, (speed / 1024 / 1024))
            elif speed > threshold:
                status = '{}: {:7.1f}MB/s'.format(traf_dir, (speed / 1024))
            else:
                status = '{}: {:7.0f}KB/s'.format(traf_dir, speed)
        elif raw == 1:
            status = '"{}" {:.0f}'.format(traf_dir.upper(), speed)
        else:
            status = '{}ld| {:.0f}'.format(traf_dir.lower(), speed)

    if debug > 0:
        print(f'DEBUG: showusers mode={mode} ucomp={ucomp} raw={raw}',
              f'username={username} x={x} hidden={chidden} showall={SHOWALL}'
              f'noshow={noshow} mask={mask} maskchar={maskchar}' )

    # show stats of users
    if mode == 0 and raw != 3:
        if raw == 0 and (SHOWALL or (not noshow and not mask and maskchar != '*')):
            if mb_xfered:
                print(string.Template(tmpl_str['upload']).substitute(tmpl_sub).format(
                    maskchar=maskchar, username=username, g_name=g_name, status=status, mb_xfered=mb_xfered
                ))
            else:
                print(string.Template(tmpl_str['download']).substitute(tmpl_sub).format(
                    maskchar=maskchar, username=username, g_name=g_name, status=status, pct=pct, bar=p_bar
                ))
            print(string.Template(tmpl_str['info']).substitute(tmpl_sub).format(
                tagline=tagline, userip=userip if userip != '0.0.0.0' else addr,  online=online, filename=filename
            ))
            print(layout['separator'])
        elif (raw == 1 and (SHOWALL or (not noshow and not mask and maskchar != '*'))):
            print('"USER" "{username}" "{g_name}" "{status}" "{tagline}" "{online}" "{filename}" "{mb_xfered}" "{currentdir}" "{procid}" "{host}" "{iso_code}" "{userip}"'.format(
                username=username, g_name=g_name, status=status, tagline=tagline, online=online, filename=filename,
                mb_xfered=mb_xfered, currentdir=currentdir, procid=user[x].procid, host=host, iso_code=iso_code, userip=userip
            ))
        elif (SHOWALL or (not noshow and not mask and maskchar != '*')):
            print("{}|{}|{}|{}|{}".format(username, g_name, tagline, status, filename))
        if (not noshow and not mask and maskchar != '*') or chidden:
            onlineusers += 1
    elif raw == 3:
        if (not noshow and not mask and maskchar != '*') or chidden:
            onlineusers += 1
    # show stats for username, if specified as command-line argument
    elif ((ucomp and username) and (ucomp == username)) and (not XXL_MODE):
        if _WITH_ALTWHO:
            if not raw and (SHOWALL or (not noshow and not mask and maskchar != '*')):
                if mb_xfered:
                    print("{} : {:1}{}/{} has xfered {:.1f}MB of {} and has been online for {:8.8}.".format(
                        status, maskchar, username, g_name, mb_xfered, filename, online
                    ))
                elif filename:
                    print("{} : {:1}{}/{} has xfered {:.0f}% of {} and has been online for {:8.8}.".format(
                        status, maskchar, username, g_name, pct, filename, online)
                    )
                else:
                    print("{} : {:1}{}/{} has been online for {:8.8s}.".format(
                        status, maskchar, username, g_name, online
                    ))
            elif raw == 1 and (SHOWALL or (not noshow and not mask and (maskchar != '*'))):
                print("\"USER\" \"{:1}\" \"{}\" \"{}\" {} \"{}\" \"{}\" \"{}\" \"{:.1f}{}\" \"{}\" \"{}\" \"{}\" \"{}\" \"{}\"".format(
                    maskchar, username, g_name, status, tagline, online, filename,
                    (pct if pct >= 0 else mb_xfered), ("%" if pct >= 0 else "MB"),
                    currentdir, user[x].procid, host, iso_code, userip
                ))
            elif SHOWALL or (not noshow and not mask and (maskchar != '*')):
                print("{}|{}|{}|{}|{}".format(username, g_name, tagline, status, filename))
        else:
            if not onlineusers:
                if not raw and (SHOWALL or (not noshow and not mask and maskchar != '*')):
                    print("\002{}\002 - {}".format(username, status))
                elif raw == 1 and (SHOWALL or (not noshow and not mask and maskchar != '*')):
                    print("\"USER\" \"{}\" {}".format(username, status))
                elif  SHOWALL or (not noshow and not mask and maskchar != '*'):
                    print("\002{}\002 - {}".format(username, status))
            else:
                if not raw and (SHOWALL or (not noshow and not mask and maskchar != '*')):
                    print(" - {}".format(status))
                elif raw == 1 and (SHOWALL or (not noshow and not mask and maskchar == '*')):
                    print("\"USER\" \"\" {}".format(status))
                elif SHOWALL and (not noshow and not mask and maskchar != '*'):
                    print(" - {}".format(status))
        if (not noshow and not mask and maskchar != '*') or chidden:
            onlineusers += 1
        filename = ""

    # xxl_mode: wide output, use columns from terminal size as width
    elif _WITH_XXL and XXL_MODE:
        upload = download = info = ''
        if p_bar:
            # add padding
            p_bar += '{:.<{width}}'.format('', width=(15-abs(len(p_bar))))
        else:
            p_bar = '-'
        filename = filename if filename else '---'
        if mb_xfered:
            upload = string.Template(tmpl_str['upload']).substitute(tmpl_sub).format(
                username=username, g_name=g_name, tagline=tagline, status=status, mb_xfered=mb_xfered
            )
            print(fmt_max_col(upload))
        else:
            download = string.Template(tmpl_str['download']).substitute(tmpl_sub).format(
                username=username, g_name=g_name, tagline=tagline, status=status.replace('  ', ' ').upper(), pct=pct, bar=p_bar
            )
            print(fmt_max_col(download))
        info = string.Template(tmpl_str['info']).substitute(tmpl_sub).format(
                userip=userip if userip != '0.0.0.0' else addr, online=online, filename=filename
        )
        print(fmt_max_col(info))
        # separator:        fmt_max_col(layout['separator'])
        # sep w/ calc len:  msg_len = max(len(upload), len(download), len(info))
        #                   print("{_m:<{col}.{col}}".format(col = min((msg_len+1)*2, columns), _m=layout['separator'] * msg_len))
        print()
        onlineusers += 1

    return dict(
        downloads=downloads, uploads=uploads, total_up_speed=total_up_speed, total_dn_speed=total_dn_speed,
        browsers=browsers, idlers=idlers, onlineusers=onlineusers, geoip2_client=geoip2_client, geoip2_shown_err=geoip2_shown_err
    )


def showtotals(*args, **kwargs):
    """ output formatted totals """
    # set variables from function parameters
    raw = args[0]
    totalusers = args[1]
    downloads = kwargs['downloads']
    uploads = kwargs['uploads']
    total_up_speed = kwargs['total_up_speed']
    total_dn_speed = kwargs['total_dn_speed']
    browsers = kwargs['browsers']
    idlers = kwargs['idlers']
    onlineusers = kwargs['onlineusers']
    if (total_up_speed > (threshold*threshold)) or (total_dn_speed > (threshold*threshold)):
        total_up_speed = (total_up_speed / 1024 / 1024)
        total_dn_speed = (total_dn_speed / 1024 / 1024)
        speed_unit = 'GB/s'
    elif (total_up_speed > threshold) or (total_dn_speed > threshold):
        total_up_speed = (total_up_speed / 1024)
        total_dn_speed = (total_dn_speed / 1024)
        speed_unit = 'MB/s'
    else:
        speed_unit = 'KB/s'
    if not raw:
        if _WITH_XXL and XXL_MODE:
            totals = string.Template(tmpl_str['totals']).substitute(tmpl_sub).format(
                uploads=uploads, total_up_speed=total_up_speed, downloads=downloads, total_dn_speed=total_dn_speed,
                total=uploads + downloads, total_speed=total_up_speed + total_dn_speed, unit=speed_unit
            )
            users = string.Template(tmpl_str['users']).substitute(tmpl_sub).format(
                onlineusers=onlineusers, maxusers=totalusers
            )
            cprint(f'{totals} {users}')
        else:
            print(string.Template(tmpl_str['totals']).substitute(tmpl_sub).format(
                uploads=uploads, total_up_speed=total_up_speed, downloads=downloads, total_dn_speed=total_dn_speed,
                total=uploads + downloads, total_speed=total_up_speed + total_dn_speed, unit=speed_unit
            ))
            print(string.Template(tmpl_str['users']).substitute(tmpl_sub).format(
                space=' ', onlineusers=onlineusers, maxusers=totalusers)
            )
    elif raw == 1:
        print('"STATS" "{uploads}" "{total_up_speed:.1f}" "{downloads}" "{total_dn_speed:.1f}" "{total}" "{total_speed:.1f}"'.format(
            uploads=uploads, total_up_speed=total_up_speed,
            downloads=downloads, total_dn_speed=total_dn_speed,
            total=uploads + downloads, total_speed=total_up_speed + total_dn_speed,
        ))
    elif raw == 3:
        print('{uploads} {total_up_speed:.1f} {downloads} {total_dn_speed:.1f} {total} {total_speed:.1f} {browsers} {idlers} {onlineusers} {maxusers}'.format(
            uploads=uploads, total_up_speed=total_up_speed, downloads=downloads, total_dn_speed=total_dn_speed,
            total=uploads + downloads, total_speed=total_up_speed + total_dn_speed,
            browsers=browsers, idlers=idlers, onlineusers=onlineusers, maxusers=totalusers
        ))


# main
#######

def main():
    """ read shm, call showusers() and showtotals() """
    if MAXUSERS == -1 and glconf_users() > 0:
        totalusers = glconf_users()
    else:
        totalusers = maxusers

    # get username from cli arg
    user_arg = None
    if USER_IDX >= 0:
        try:
            user_arg = sys.argv[USER_IDX]
        except (KeyError, IndexError):
            pass

    # init screen drawing related vars
    repeat = 0
    user_action = 0         # 1=userinfo 2=kill user 3=other
    screen_redraw = 0       # 1=redraw logo/header

    # clear screen
    if _WITH_SPY and SPY_MODE:
        print(f"{cur('2J')}{cur('H')}")

    # show logo with header
    if (len(sys.argv) == 1 and not RAW_OUTPUT) or (_WITH_SPY and SPY_MODE):
        print(layout['header'])
    elif _WITH_XXL and XXL_MODE:
        print('\n[ PY-WHO ]\n')

    # loop: if in spymode keep repeating indefinitely,
    #       for non-spy modes quit after single iteration
    userdata = []
    u_idx = 0
    geoip2_shown_err = 0
    while (_WITH_SPY and SPY_MODE) or (not SPY_MODE and repeat < 1):
        if debug == 0:
            try:
                memory = sysv_ipc.SharedMemory(KEY, flags=sysv_ipc.SHM_RDONLY, mode=0)
            except sysv_ipc.ExistentialError as shm_err:
                if not RAW_OUTPUT:
                    print(f"Error: {shm_err} (0x{KEY:08X})\n{' ':7.7}No users are logged in?\n")
                else:
                    print(f'"ERROR" "No users logged in?" "{shm_err}" "0x{KEY:08X}"')
                sys.exit(1)
        else:
            memory = sysv_ipc.SharedMemory(KEY, flags=sysv_ipc.SHM_RDONLY, mode=0)
        buf = memory.read()

        # spy mode: on redraw first clear screen, then show logo/header,
        #           move cursor up using ansi escape codes and show user[x] lines
        if repeat > 0 and user_action == 0:
            # debug: print vars, sleep to be able to view them
            if debug > 4:
                print(f'DEBUG: spy vars user_action={user_action} screen_redraw={screen_redraw}')
                time.sleep(2)
            if screen_redraw == 0:
                # go back up and clear 'l' lines per user + totals + usage lines
                # len(layout['header'].splitlines())
                l = (len(userdata) * 3) if userdata else 0
                print(f"{cur('F', l+3+2)}")
                print(f"{cur('0J')}{cur('F',2)}", end="")
            else:
                print(f"{cur('2J')}{cur('H')}", end="")
                print(layout['header'])
                screen_redraw = 0

        # reset user data for every repeat
        userdata = []
        u_idx = 0

        kwargs = dict(
            downloads=DOWNLOADS, uploads=UPLOADS,
            total_up_speed=TOTAL_UP_SPEED, total_dn_speed=TOTAL_DN_SPEED,
            browsers=BROWSERS, idlers=IDLERS, onlineusers=ONLINEUSERS,
            geoip2_client = GEOIP2_CLIENT if GEOIP2_CLIENT else None,
            geoip2_shown_err = geoip2_shown_err if geoip2_shown_err else 0
        )

        # user loop: unpack shm in 'buf' as py struct, loop over struct.iter (904 bytes)
        #            make tuples in a list called 'userdata', skip if empty
        for user_tuple in struct.iter_unpack(STRUCT_FMT, buf):
            if struct_ONLINE._make(user_tuple).procid:
                userdata.insert(u_idx, struct_ONLINE._make(user_tuple))
                if user_action == 0:
                    if debug > 2:
                        print(f'DEBUG: user loop sys.argv={sys.argv} (len={len(sys.argv)})',
                              f'user_idx={USER_IDX} user_arg={user_arg} raw_output={RAW_OUTPUT}',
                              f'repeat={repeat} u_idx={u_idx} chidden={CHIDDEN}')
                    if RAW_OUTPUT < 2:
                        kwargs = showusers(
                            userdata, len(sys.argv) - RAW_OUTPUT - 1, user_arg, RAW_OUTPUT, repeat, u_idx, CHIDDEN, **kwargs
                        )
                    elif len(sys.argv) == 1:
                        kwargs = showusers(
                            userdata, len(sys.argv) - 1, user_arg, RAW_OUTPUT, repeat, u_idx, CHIDDEN, **kwargs
                        )
                    elif RAW_OUTPUT == 3:
                        kwargs = showusers(
                            userdata, len(sys.argv) - 2, user_arg, RAW_OUTPUT, repeat, u_idx, CHIDDEN, **kwargs
                        )
                    else:
                        kwargs = showusers(
                            userdata, 0, user_arg, RAW_OUTPUT, repeat, u_idx, CHIDDEN, **kwargs
                        )

                u_idx += 1

                if _WITH_SPY and SPY_MODE:
                    hdr_lines = layout['header'].count('\n')
                    if ((u_idx * 3) + hdr_lines > os.get_terminal_size().lines and screen_redraw == 0):
                        time.sleep(1)
                        screen_redraw = 1

        # make sure we do not show geoip2 error msgs more than once
        if _WITH_SPY and geoip2_enable:
            geoip2_shown_err = kwargs['geoip2_shown_err']

        # show totals or single user stats
        if user_action == 0:
            if (len(sys.argv) == 1) or (RAW_OUTPUT == 3) or (_WITH_SPY and SPY_MODE) or (_WITH_XXL and XXL_MODE):
                showtotals(RAW_OUTPUT, totalusers, **kwargs)
                if not RAW_OUTPUT and not XXL_MODE:
                    print(layout['footer'])
            elif user_arg and not XXL_MODE:
                u_found = False
                arg_idx = 0
                while arg_idx < len(userdata):
                    if userdata[arg_idx].username.split(NULL_CHAR, 1)[0].decode() == user_arg:
                        u_found = True
                        break
                    arg_idx += 1
                if not u_found:
                    if not RAW_OUTPUT:
                        print(f"\002{user_arg}\002 is not online\n")
                    else:
                        print(f"\"ERROR\" \"User {user_arg} not online.\"\n")
                    sys.exit(1)
            if (_WITH_ALTWHO and not RAW_OUTPUT) or (_WITH_XXL and XXL_MODE):
                print()

        # spy-mode: handle keyboard input
        if _WITH_SPY and SPY_MODE:
            signal.signal(signal.SIGINT, spy_break)
            if user_action == 0:
                spy_usage(u_idx)
            [ user_action, screen_redraw ] = spy_input_action(userdata, u_idx, user_action, screen_redraw)
            if user_action == 0:
                time.sleep(1)
            elif _WITH_GEOIP and geoip2_enable:
                time.sleep(2)

        repeat += 1

    try:
        memory.detach()
    except (UnboundLocalError, sysv_ipc.Error):
        pass
    if _WITH_GEOIP and geoip2_enable:
        GEOIP2_CLIENT.close()
    sys.exit(0)

if __name__ == "__main__":
    main()


# fuquallkthnxbye.
