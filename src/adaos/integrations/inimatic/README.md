# Inimatic

Inimatic - authorization system

# Development Workflow
## Requirements
Nodejs 18

## Installation
1. Install Android Studio
2. Install [JDK](https://www.oracle.com/java/technologies/downloads/)
3. Set System Path 
4. $env:NODE_OPTIONS="--openssl-legacy-provider"
Before proceeding, make sure the latest version of Node.js and npm are installed. See Environment Setup for details. Install the Ionic CLI with npm:
```
npm i -g @ionic/cli
npm i -g cordova
npm i -g native-run # for run Android app in dev mode
npm i
npm add.android
npm add.plugin
```
5. Setup [Redis on Windows](https://github.com/microsoftarchive/redis/releases), Redis on Ubuntu:
```
sudo apt update
sudo apt install redis-server -y
sudo systemctl enable --now redis-server
```

## Setup build Android

1. Path to SDK 
Create new file --> Project--> Platform --> android--> local.properties
and keep this --> sdk.dir=/path/sdk example [sdk.dir=/Users/admin/Library/Android/sdk]
2. Android Studio/Settings/Android SDK/
> https://stackoverflow.com/questions/67398608/unable-to-determine-android-sdk
- Android SDK Build-Tools 33.0.2 version 33.0.2
3. Install
- Sources for Android 32 (sources;android-32)

## Setup Android build env
https://forum.ionicframework.com/t/how-to-create-build-apk-file-for-android-from-ionic-project/230258/7


# https://stackoverflow.com/questions/43480076/ionic-2-error-could-not-find-an-installed-version-of-gradle-either-in-android
* Min SDK 33
* Used OpenJDK v11.0.0.2, Android SDK 33, Gradle v7.6.2
```
export GRADLE_PATH="/home/teacher/.gradle/wrapper/dists/gradle-8.0.2-all/25ipb77ce0ypy3f9xdton1ae6/gradle-8.0.2/bin/"
export ANDROID_HOME=/home/teacher/Android/Sdk
export PATH=${PATH}:$ANDROID_HOME/tools:$ANDROID_HOME/platform-tools:$GRADLE_PATH
export PATH="$PATH:$GRADLE_PATH"
```

## Deployment

See [`deployment/README.md`](deployment/README.md) for the Traefik-based Docker Compose setup, production rollout guide, and CI/CD pipeline details.

## Run Android app in development mode
```
npm run dev.android [--address <ip address> -p=<port>]
```

## Build Android app  
```
npm run build.android
```

## Add Browser Extension  
1. Open ```chrome://extensions``` for Chrome, ```browser://extensions``` for Yandex, ```edge://extensions``` for Edge
2. Enable Developer Mode by clicking the toggle switch next to Developer mode
3. Click the "Load unpacked" button and select the ```./browser_extension``` in the project root

## IONIC documentation
[IONIC documentation](https://test-bc740.web.app/)

## Support

Drop us a line to logdevel@stipot.com

## Acknowledgements

Thank to ionicthemes.com team.

### Committing code

To ensure code quality, we follow and enforce the [Angular Commit Message Guidelines](https://github.com/angular/angular/blob/master/CONTRIBUTING.md#-commit-message-guidelines)
These guidelines define a Commit Message Format and certain rules that will help teams achieve consistency with version control and source code management practices.
Branch management guide: [A successful Git branching model](https://nvie.com/posts/a-successful-git-branching-model/)

#### Commit Message Format

Each commit message consists of a **header**, a **body** and a **footer**. The header has a special
format that includes a **type**, a **scope** and a **subject**:

```
<type>(<scope>): <subject>
<BLANK LINE>
<body>
<BLANK LINE>
<footer>
```

The **header** is mandatory and the **scope** of the header is optional.

Any line of the commit message cannot be longer 100 characters! This allows the message to be easier
to read on GitHub as well as in various git tools.

The footer should contain a [closing reference to an issue](https://help.github.com/articles/closing-issues-via-commit-messages/) if any.

Samples: (even more [samples](https://github.com/angular/angular/commits/master))

```
docs(changelog): update changelog to beta.5
```

```
fix(release): need to depend on latest rxjs and zone.js

The version in our package.json gets copied to the one we publish, and users need the latest of these.
```

#### Revert

If the commit reverts a previous commit, it should begin with `revert: `, followed by the header of the reverted commit. In the body it should say: `This reverts commit <hash>.`, where the hash is the SHA of the commit being reverted.

#### Type

Must be one of the following:

- **build**: Changes that affect the build system or external dependencies (example scopes: gulp, broccoli, npm)
- **ci**: Changes to our CI configuration files and scripts (example scopes: Travis, Circle, BrowserStack, SauceLabs)
- **docs**: Documentation only changes
- **feat**: A new feature
- **fix**: A bug fix
- **perf**: A code change that improves performance
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **style**: Changes that do not affect the meaning of the code (white-space, formatting, missing semi-colons, etc)
- **test**: Adding missing tests or correcting existing tests

#### Scope

The scope should be the name of the npm package affected (as perceived by the person reading the changelog generated from commit messages.

The following is the list of supported scopes:

- **android**
- **iOS**
- **web**
- **code**
- **extention**
- **components**
- **profile**
- **comporision**
- **integration**
- **data**
- **auth**

There are currently a few exceptions:

- **packaging**: used for changes that change the npm package layout in all of our packages, e.g.
  public path changes, package.json changes done to all packages, d.ts file/format changes, changes
  to bundles, etc.
- **changelog**: used for updating the release notes in CHANGELOG.md
- none/empty string: useful for `style`, `test` and `refactor` changes that are done across all
  packages (e.g. `style: add missing semicolons`) and for docs changes that are not related to a
  specific package (e.g. `docs: fix typo in tutorial`).

#### Subject

The subject contains a succinct description of the change:

- use the imperative, present tense: "change" not "changed" nor "changes"
- don't capitalize the first letter
- no dot (.) at the end

#### Body

Just as in the **subject**, use the imperative, present tense: "change" not "changed" nor "changes".
The body should include the motivation for the change and contrast this with previous behavior.

#### Footer

The footer should contain any information about **Breaking Changes** and is also the place to
reference GitHub issues that this commit **Closes**.

**Breaking Changes** should start with the word `BREAKING CHANGE:` with a space or two newlines. The rest of the commit message is then used for this.

## Troubleshooting

### See what dependencies and versions you have installed in your project

This is useful to track compilation ERRORS

- Run `npm ls` to list all installed packages
- To find the installed version of a specific package run `npm list package_name` (ex: `npm list @ionic/core`)
- To find out which packages need to be updated, you can use `npm outdated -g --depth=0`
- In particular, run `ng version` to output Angular CLI version and all Angular related installed packages and versions
