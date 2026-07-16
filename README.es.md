# Tricloud

[English](README.md) | [日本語](README.ja.md) | [Español](README.es.md)

Tricloud es una aplicación beta de almacenamiento en la nube para Windows que incorpora ideas de Web3 y del almacenamiento descentralizado.

Tricloud se desarrolla a partir de una pregunta sencilla: ¿el almacenamiento en la nube debe depender únicamente de grandes proveedores centralizados?

Los usuarios pueden utilizar almacenamiento en la nube y también aportar a la red parte del espacio libre de sus propios ordenadores. A largo plazo, Tricloud pretende convertirse en un servicio en el que muchos usuarios aporten capacidad y puedan recibir recompensas según factores como la cantidad proporcionada y la disponibilidad.

La beta pública actual incluye funciones básicas de almacenamiento en la nube y un modo experimental para proveedores de almacenamiento.

> \*\*Estado:\*\* Beta pública / prototipo inicial  
> \*\*Versión:\*\* `v0.1.0-beta`  
> \*\*Sistema compatible:\*\* Windows  
> \*\*Categoría:\*\* Almacenamiento en la nube inspirado en Web3 y el almacenamiento descentralizado  
> \*\*Estabilidad:\*\* Experimental  
> \*\*Uso recomendado:\*\* Solo para pruebas  
> \*\*Importante:\*\* No utilices esta beta como la única ubicación de archivos importantes o irremplazables.

\---

## ⚠️ Importante: esta beta no paga recompensas

# **Durante la beta actual, los proveedores de almacenamiento no reciben dinero, criptomonedas, tokens ni ninguna otra recompensa.**

La prueba del modo proveedor sirve únicamente para comprobar el funcionamiento técnico, la facilidad de uso, la conectividad y la estabilidad.

Participar en la beta, aportar almacenamiento o mantener un nodo conectado no genera ningún derecho a una compensación futura.

\---

## Relación entre Tricloud y Web3

Tricloud pretende permitir que los usuarios aporten el espacio libre de sus ordenadores para que otros usuarios puedan utilizarlo como almacenamiento en la nube.

Incorpora ideas relacionadas con Web3 y los servicios descentralizados de las siguientes maneras:

* Los usuarios pueden aportar capacidad de almacenamiento a la red.
* Una persona puede participar como usuaria y como proveedora de almacenamiento.
* La capacidad no tiene que proceder únicamente de un gran operador.
* El servicio puede utilizar almacenamiento aportado por múltiples participantes.
* En el futuro podría introducirse un sistema de recompensas para proveedores.

Sin embargo, **en Tricloud solo están descentralizados el suministro y el uso del almacenamiento**.

La autenticación, las cuentas, los metadatos de archivos, la gestión de nodos, la asignación de almacenamiento y otras funciones de coordinación son gestionadas por un servidor central. No existe un plan para descentralizar completamente todo el servicio.

Por tanto, Tricloud no es una dApp completamente descentralizada. Es un servicio de almacenamiento en la nube que aplica ideas de Web3 y almacenamiento descentralizado específicamente al suministro y uso del almacenamiento.

\---

## Tecnologías relacionadas con blockchain

Tricloud no utiliza actualmente:

* Blockchain
* Un token propio
* Una cartera de criptomonedas para usuarios
* Contratos inteligentes
* NFT
* Gobernanza mediante DAO
* Gestión de cuentas o archivos en una blockchain

Estas tecnologías no se consideran necesarias para el servicio Tricloud actual y no está previsto implementarlas dentro del desarrollo futuro normalmente contemplado.

Tampoco está previsto trasladar a blockchain o a contratos inteligentes el servidor de gestión, la autenticación, los metadatos, la asignación de almacenamiento ni el cálculo de recompensas.

Si algún día Tricloud ampliara su actividad a ordenadores, teléfonos u otra plataforma propia sustancialmente diferente, podrían reconsiderarse las tecnologías necesarias. Sin embargo, dentro del servicio de almacenamiento en la nube que se desarrolla actualmente, no está previsto introducir blockchain, un token propio, una cartera dedicada ni contratos inteligentes.

\---

### Posible compatibilidad con criptomonedas existentes

Si las criptomonedas existentes llegan a ser tan flexibles como las monedas ordinarias desde el punto de vista legal, técnico y operativo, Tricloud podría considerar su uso como método opcional para pagar almacenamiento o recibir recompensas.

Esto solo añadiría otro método de pago o cobro junto a monedas ordinarias, tarjetas o transferencias bancarias. No significaría:

* Emitir un token de Tricloud
* Proporcionar una cartera de Tricloud
* Operar el servicio mediante contratos inteligentes
* Guardar datos de archivos o cuentas en una blockchain
* Convertir Tricloud en una aplicación blockchain

\---

## Descarga

La versión `v0.1.0-beta` para Windows se distribuirá mediante GitHub Releases.

* **Instalador:** Formato de distribución admitido para esta versión
* **Versión Portable:** No se incluye porque la versión probada falla al iniciarse y muestra `Invalid file descriptor to ICU data received.`

El instalador incluye los archivos backend y el entorno de ejecución de Python necesarios. Normalmente, los usuarios no necesitan instalar Python ni paquetes de Python por separado.

En los dos entornos probados, la instalación y el primer inicio tardaron solo unos segundos. El tiempo real puede variar según el ordenador.

La beta no está firmada digitalmente, por lo que Windows SmartScreen muestra una advertencia al ejecutar el instalador.

\---

## Funciones disponibles en esta beta

La beta actual permite probar:

* Creación de cuentas
* Inicio de sesión desde la aplicación de escritorio
* Subida de archivos
* Descarga de archivos
* Creación y gestión de archivos y carpetas
* Interfaz de unidad en la nube para escritorio
* Copias de seguridad automáticas desde la página de configuración
* Uso sin conexión de archivos y carpetas
* Configuración de la cantidad de almacenamiento aportada
* Inicio y detención de un nodo proveedor
* Funciones experimentales de proveedor de almacenamiento
* Supervisión del estado del nodo
* Conexiones HTTPS con el servidor beta de Tricloud
* Reconexión después de una interrupción temporal de red
* Ejecución del instalador en otro ordenador con Windows

Algunas funciones pueden seguir incompletas, ser inestables o no estar disponibles temporalmente.

\---

## Modo proveedor de almacenamiento

El modo proveedor permite aportar a la red de pruebas de Tricloud una parte del espacio libre del ordenador.

El flujo actual es:

1. Introducir la cantidad de almacenamiento que se desea aportar.
2. Guardar la configuración.
3. Iniciar la aportación de almacenamiento.
4. Comprobar el estado del nodo.
5. Detener la aportación cuando sea necesario.

La distribución incluye el entorno de Python y el backend del nodo, por lo que normalmente no es necesario preparar Python manualmente.

Si la conexión a Internet o Wi-Fi se interrumpe temporalmente mientras el nodo está activo, este intenta volver a conectarse al DataServer cuando regresa la conexión. Dependiendo de la red, el nodo puede tardar en volver a aparecer como conectado.

### Recompensas durante la beta

# **Durante la beta actual no se paga ninguna recompensa por aportar almacenamiento.**

La prueba tiene como objetivo comprobar:

* Si el nodo puede iniciarse en otros ordenadores con Windows
* Si la aportación puede iniciarse y detenerse correctamente
* Si el nodo vuelve a conectarse después de una interrupción de red
* Si la capacidad aportada se reconoce correctamente
* Si el nodo puede permanecer conectado durante periodos prolongados
* Si el proceso de configuración es comprensible

Participar en la beta no garantiza ningún pago futuro.

\---

## Funciones todavía no completadas

Las siguientes áreas siguen siendo experimentales o no están preparadas para uso general:

* Funciones de proveedor listas para producción
* Pagos a proveedores de almacenamiento
* Cálculo y distribución de recompensas en producción
* Incorporación de pagos mediante Stripe Connect
* Aplicaciones móviles
* Aplicaciones de escritorio para macOS y Linux
* Soporte de usuario de nivel de producción
* Garantías de almacenamiento a largo plazo
* Auditorías de seguridad independientes
* Pruebas de rendimiento a gran escala

Blockchain, los tokens propios, las carteras y los contratos inteligentes no son funciones pendientes. Son tecnologías que Tricloud no tiene previsto implementar actualmente.

La prueba del modo proveedor está disponible, pero no se paga ninguna recompensa durante la beta.

Esta beta debe considerarse una vista previa técnica y no un servicio de almacenamiento de producción.

\---

## Arquitectura actual

La arquitectura actual de Tricloud incluye, en términos generales:

* Una aplicación de escritorio para Windows creada con Electron, React, TypeScript y Tailwind CSS
* Un entorno de ejecución de Python incluido
* Un backend incluido para el nodo proveedor
* Una Control API desarrollada con FastAPI
* PostgreSQL para cuentas y metadatos de archivos
* Un DataServer para las comunicaciones relacionadas con el almacenamiento
* Nginx para el acceso HTTPS
* ZeroMQ para la comunicación entre DataServer y nodos
* Un servidor de gestión beta en una máquina virtual de Google Cloud
* Nodos proveedores ejecutados en los ordenadores Windows de los usuarios

Tricloud combina un servidor central de gestión con nodos de almacenamiento aportados por usuarios.

El servidor central gestiona autenticación, metadatos, información de nodos, asignación de almacenamiento y coordinación. Los nodos de usuarios aportan capacidad para los datos de archivos.

No está previsto sustituir las funciones centrales por tecnología blockchain.

\---

## Instalación

### Instalador (`v0.1.0-beta`)

1. Descarga el instalador desde GitHub Releases.
2. Ejecuta el instalador.
3. Si Windows SmartScreen muestra una advertencia, comprueba que el archivo procede de la versión oficial de Tricloud en GitHub antes de decidir si deseas continuar.
4. Espera mientras se instalan la aplicación, el backend y el entorno.
5. Inicia Tricloud desde el menú Inicio o el acceso directo del escritorio.
6. Crea una cuenta o inicia sesión.
7. Prueba a subir y descargar un archivo pequeño que no contenga datos sensibles.

### Versión Portable

La versión Portable actual no se inicia correctamente y muestra `Invalid file descriptor to ICU data received.`, por lo que no se distribuye con `v0.1.0-beta`.

### Python

Normalmente no es necesario instalar Python por separado.

El instalador incluye el entorno de Python y los archivos backend necesarios para el modo proveedor.

Las versiones antiguas de prueba pueden no incluir todos los archivos necesarios. Utiliza la versión más reciente de GitHub Releases.

\---

## Entornos probados

`v0.1.0-beta` se ha probado en dos ordenadores Windows 11 x64 con:

* 12th Gen Intel(R) Core(TM) i5-1240P
* Intel(R) Core(TM) i3-7020U

En esos entornos se confirmó:

* Instalación y desinstalación
* Inicio en unos pocos segundos
* Creación de cuentas e inicio de sesión
* Subida de archivos pequeños y de tamaño moderado
* Descarga de archivos cuyo contenido coincide con el original
* Reflejo de cambios de archivos y carpetas mediante copia de seguridad automática
* Reflejo de cambios realizados sin conexión después de volver a conectarse
* Inicio, detención, reinicio y reconexión de un nodo proveedor
* Funcionamiento normal de la aplicación después de cerrar completamente y reiniciar Tricloud

Windows 10, Windows en ARM64 y otros entornos todavía no se han verificado. Estos procesadores representan entornos de prueba y no requisitos mínimos.

\---

## Lista de comprobación inicial

Después de iniciar la aplicación:

1. Crea una cuenta nueva.
2. Inicia sesión.
3. Sube un archivo pequeño de prueba.
4. Descarga el archivo.
5. Cierra la aplicación.
6. Iníciala de nuevo.
7. Inicia sesión y confirma que sigue apareciendo la lista de archivos.

Para probar el modo proveedor:

1. Introduce una cantidad de almacenamiento.
2. Guarda la configuración.
3. Inicia la aportación.
4. Confirma que el nodo aparece conectado.
5. Detén la aportación.
6. Iníciala de nuevo.
7. Si es posible, desconecta temporalmente el Wi-Fi.
8. Comprueba si el nodo vuelve a conectarse al restablecerse el Wi-Fi.

Utiliza un entorno de prueba que no contenga datos importantes.

\---

## Informar de un problema

Abre un Issue en GitHub e incluye:

* Versión de Windows
* Versión de Tricloud
* Confirma que utilizaste el instalador de `v0.1.0-beta`
* Qué intentabas hacer
* Qué ocurrió realmente
* Mensaje de error visible
* Acción realizada inmediatamente antes del problema
* Si el nodo estaba conectado o desconectado, cuando corresponda
* Si se produjo una interrupción o reconexión de red

No incluyas:

* Contraseñas
* Tokens de autenticación
* Claves de API
* Información personal
* Contenido de archivos
* Claves privadas u otra información confidencial

\---

## Limitaciones conocidas

* El único entorno confirmado actualmente es Windows 11 x64. Windows 10 y Windows en ARM64 no se han verificado.
* La aplicación no está firmada y Windows SmartScreen muestra una advertencia.
* La versión Portable no se incluye en `v0.1.0-beta` porque actualmente falla al iniciarse con `Invalid file descriptor to ICU data received.`
* El primer inicio y la preparación del entorno pueden tardar más en ordenadores no probados.
* Microsoft Defender no bloqueó la aplicación en los dos entornos probados, pero otros equipos o productos de seguridad pueden inspeccionar o bloquear Python o el proceso del nodo.
* El servidor de gestión puede no estar disponible temporalmente por mantenimiento, actualizaciones, reinicios o fallos.
* Los cambios de la beta o las migraciones de la base de datos pueden requerir que las cuentas o los datos de prueba almacenados se eliminen o restablezcan.
* No existe ningún SLA ni garantía de disponibilidad, duración del almacenamiento, recuperación o restauración de datos.
* El comportamiento de subida, descarga, copia de seguridad automática y uso sin conexión puede cambiar durante la beta.
* El modo proveedor es experimental.
* **Durante la beta actual no se paga ninguna recompensa por aportar almacenamiento.**
* La reconexión después de una interrupción puede tardar.
* Algunos elementos de la interfaz y mensajes de error están incompletos.
* No se ha probado completamente el rendimiento a gran escala.
* Tricloud no es un sistema totalmente descentralizado.
* No utilices la beta para datos importantes, sensibles o irremplazables.
* No utilices Tricloud como la única copia de ningún archivo importante.

\---

## Seguridad y privacidad

Tricloud sigue siendo una beta inicial.

No subas:

* Archivos con información personal
* Archivos altamente confidenciales
* Secretos comerciales
* Archivos cuya pérdida resultaría grave
* Archivos que no existan en ningún otro lugar

El servidor beta se utiliza para comprobar el flujo básico del servicio. El refuerzo adicional de seguridad y una revisión independiente siguen siendo tareas futuras.

\---

## Tecnologías utilizadas

* Electron
* React
* TypeScript
* Tailwind CSS
* Python
* FastAPI
* PostgreSQL
* Nginx
* Google Cloud
* ZeroMQ
* electron-builder

Las siguientes tecnologías no se utilizan y no están previstas actualmente:

* Blockchain
* Tokens propios
* Carteras
* Contratos inteligentes
* NFT
* DAO

\---

## Hoja de ruta

El trabajo previsto incluye:

* Mejorar la fiabilidad de inicio en otros ordenadores Windows
* Mejorar la preparación del entorno Python
* Estabilizar las subidas y descargas
* Mejorar la reconexión tras interrupciones
* Mejorar errores y registros
* Ampliar la gestión de archivos de escritorio
* Mejorar el modo proveedor
* Mejorar la medición de capacidad y disponibilidad
* Diseñar las recompensas para proveedores
* Implementar pagos
* Mejorar las páginas de cuenta y uso
* Reforzar la seguridad
* Considerar versiones para macOS y Linux
* Crear documentación para colaboradores y desarrolladores
* Considerar criptomonedas existentes como método opcional de pago o cobro únicamente si resultan suficientemente prácticas

Aunque se añadiera compatibilidad con criptomonedas, Tricloud no tiene previsto introducir un token propio, una cartera dedicada ni contratos inteligentes.

\---

## Comentarios solicitados

### 1\. Funcionamiento en otros ordenadores

* ¿Se inició la aplicación sin instalar Python manualmente?
* ¿Utilizaste el instalador de `v0.1.0-beta`?
* ¿Cuánto tardó el primer inicio?
* ¿Windows Defender u otro software bloqueó algún componente?
* ¿Funcionó al instalarla o extraerla en otra ubicación?
* ¿Siguió funcionando después de reiniciarla?
* ¿Los mensajes de error fueron comprensibles?

### 2\. Funciones principales

* ¿Funcionaron el registro y el inicio de sesión?
* ¿Funcionaron la subida y la descarga?
* ¿Fueron comprensibles las operaciones de archivos y carpetas?
* ¿Funcionó la copia de seguridad automática como esperabas?
* ¿Fue comprensible el uso sin conexión?
* ¿El rendimiento fue aceptable y estable?

### 3\. Interfaz y traducciones

* ¿La estructura de la pantalla es comprensible?
* ¿Los botones y menús están situados de forma natural?
* ¿Los textos son claros?
* ¿La aplicación se siente natural como aplicación de escritorio para Windows?
* **Informa de cualquier traducción de la interfaz en japonés, inglés o español que sea incorrecta, poco natural o difícil de entender.**

### 4\. Ubicación de la copia de seguridad automática

La copia de seguridad automática se gestiona actualmente principalmente desde la página de configuración.

¿Debería estar disponible también en el menú contextual de archivos y carpetas?

* Mantenerla solo en la página de configuración
* Añadirla también al menú contextual
* Ofrecerla en ambos lugares
* Utilizar otro método de interacción

### 5\. Modo proveedor de almacenamiento

* ¿Fue comprensible la configuración?
* ¿Pudiste iniciar el nodo sin instalar Python por separado?
* ¿La cantidad aportada fue fácil de entender?
* ¿Fue fácil iniciar y detener la aportación?
* ¿El estado del nodo fue comprensible?
* ¿El nodo se reconectó después de interrumpir el Wi-Fi?
* ¿Las explicaciones y advertencias fueron suficientes?
* ¿Utilizarías esta función si hubiera recompensas en el futuro?

### 6\. Posible modelo futuro de recompensas

Se están considerando dos modelos:

1. Modelo medido según la capacidad aportada y la disponibilidad
2. Modelo similar a una lotería, donde la capacidad y disponibilidad afectan a la probabilidad de recibir una recompensa

¿Cuál te resulta más atractivo? También puedes proponer otro modelo.

Un sistema similar a una lotería puede plantear problemas legales o normativos según el país o la región. Aunque reciba más apoyo, Tricloud podría tener que adoptar el modelo medido después de una revisión legal.

\---

## Contribuir

Tricloud sigue siendo una beta inicial y su estructura interna puede cambiar considerablemente. Se agradecen los informes de errores y los comentarios.

Son especialmente útiles:

* Pruebas en otros ordenadores Windows
* Resultados de instalación y desinstalación del instalador
* Errores
* Problemas de interfaz
* Problemas de instalación
* Problemas del entorno Python
* Textos o traducciones poco claros
* Problemas de rendimiento y estabilidad
* Problemas de reconexión
* Opiniones sobre la ubicación de la copia de seguridad automática
* Opiniones sobre el modo proveedor
* Opiniones sobre futuros modelos de recompensas

\---

## Licencia

Licencia: por determinar

Se seleccionará una licencia formal antes de una publicación más amplia.

Hasta que se seleccione una licencia, no debe asumirse que existe permiso para utilizar, redistribuir o modificar el código fuente.

\---

## Aviso legal

Tricloud es software experimental. Utilízalo bajo tu propia responsabilidad.

No utilices esta beta como la única ubicación de archivos importantes.

El modo proveedor y los futuros modelos de recompensas incluyen conceptos que todavía se están probando.

Participar en la beta, aportar almacenamiento o mantener un nodo conectado no garantiza pagos futuros, distribución de tokens, criptomonedas ni ningún otro beneficio.

Tricloud no tiene previsto emitir un token propio.

