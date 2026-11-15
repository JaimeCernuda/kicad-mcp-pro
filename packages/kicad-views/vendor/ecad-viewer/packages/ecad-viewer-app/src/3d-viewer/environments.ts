// kicad-views: only the environments that need no download. The upstream list adds two
// HDR maps from storage.googleapis.com, which a view with an empty CSP cannot load.
export const environments = [
	{
		id: '',
		name: 'None',
		path: null,
	},
	{
		id: 'neutral', // THREE.RoomEnvironment
		name: 'Neutral',
		path: null,
	},
];
